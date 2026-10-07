"""模拟盘数据模型

与回测的关键区别
----------------
``strategy/engine.py`` 的 ``BacktestEngine`` 是**一次性遍历**设计，
持仓是一个局部整数变量。模拟盘必须**跨日持久化状态**并管理订单生命周期，
因此数据模型独立，不复用回测的 Trade。

设计要点见 ``docs/phase4_prd_paper_trading.md``。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class Side(str, Enum):
    """买卖方向"""

    BUY = "buy"
    SELL = "sell"


class OrderStatus(str, Enum):
    """订单状态

    生命周期: PENDING -> FILLED | REJECTED | CANCELLED
    """

    PENDING = "pending"  # 已挂单, 等待 T+1 撮合
    FILLED = "filled"  # 已成交
    REJECTED = "rejected"  # 已拒绝(资金不足/T+1限制/涨跌停)
    CANCELLED = "cancelled"  # 已取消


@dataclass
class Position:
    """持仓

    Attributes:
        symbol: 股票代码
        shares: 持股总数
        avg_cost: 移动加权平均成本价
        last_price: 最新价(用于估值)
        open_date: 建仓日(ISO 格式), 用于 T+1 判定
        frozen_shares: 当日买入被冻结的股数(A股 T+1 当日不可卖)
    """

    symbol: str
    shares: int = 0
    avg_cost: float = 0.0
    last_price: float = 0.0
    open_date: str = ""
    frozen_shares: int = 0

    @property
    def available_shares(self) -> int:
        """可卖股数(扣除当日买入的冻结部分)"""
        return max(0, self.shares - self.frozen_shares)

    @property
    def market_value(self) -> float:
        """市值"""
        return self.shares * self.last_price

    @property
    def cost_value(self) -> float:
        """持仓成本"""
        return self.shares * self.avg_cost

    @property
    def profit_loss(self) -> float:
        """浮动盈亏金额"""
        return self.market_value - self.cost_value

    @property
    def profit_loss_pct(self) -> float:
        """浮动盈亏比例"""
        if self.avg_cost <= 0:
            return 0.0
        return (self.last_price - self.avg_cost) / self.avg_cost

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Position:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class Order:
    """订单

    订单在 T 日收盘后由策略信号生成, 于 T+1 日开盘撮合。
    延迟一个交易日是**防止未来函数**的关键设计。

    Attributes:
        symbol: 股票代码
        side: 买卖方向
        shares: 委托数量
        signal_action: 产生该订单的策略动作
        signal_confidence: 策略信心度
        signal_reason: 触发原因(用于事后复盘)
        created_date: 订单创建日(ISO), 即信号日
        status: 订单状态
        reject_reason: 拒单原因
        trade_id: 成交后关联的成交 ID
    """

    symbol: str
    side: Side
    shares: int
    signal_action: str = ""
    signal_confidence: float = 0.0
    signal_reason: str = ""
    created_date: str = ""
    status: OrderStatus = OrderStatus.PENDING
    reject_reason: str = ""
    trade_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Order:
        return cls(
            symbol=data["symbol"],
            side=Side(data["side"]),
            shares=int(data["shares"]),
            signal_action=data.get("signal_action", ""),
            signal_confidence=data.get("signal_confidence", 0.0),
            signal_reason=data.get("signal_reason", ""),
            created_date=data.get("created_date", ""),
            status=OrderStatus(data.get("status", "pending")),
            reject_reason=data.get("reject_reason", ""),
            trade_id=data.get("trade_id", ""),
        )


@dataclass
class Trade:
    """成交记录

    每次实际成交都落一条完整记录, 含各项费用明细, 便于事后核对成本。
    """

    trade_id: str
    symbol: str
    side: Side
    shares: int
    price: float
    amount: float  # 成交金额 = price * shares
    commission: float = 0.0
    stamp_tax: float = 0.0  # 印花税(仅卖出)
    transfer_fee: float = 0.0
    slippage_cost: float = 0.0
    signal_source: str = ""
    signal_confidence: float = 0.0
    executed_at: str = ""  # 成交日(ISO)

    @property
    def total_fee(self) -> float:
        """总费用"""
        return self.commission + self.stamp_tax + self.transfer_fee

    @property
    def cash_flow(self) -> float:
        """对现金的影响: 买入为负, 卖出为正"""
        if self.side == Side.BUY:
            return -(self.amount + self.total_fee)
        return self.amount - self.total_fee

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["side"] = self.side.value
        d["total_fee"] = round(self.total_fee, 4)
        d["cash_flow"] = round(self.cash_flow, 4)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Trade:
        fields = cls.__dataclass_fields__
        return cls(
            **{k: v for k, v in data.items() if k in fields and k != "side"},
            side=Side(data["side"]),
        )


@dataclass
class EquityPoint:
    """资金曲线上的一个点(每交易日收盘记录)"""

    date: str
    total_asset: float
    cash: float
    market_value: float
    drawdown: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EquityPoint:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class Account:
    """模拟账户

    不变量(必须有测试锁定):
      1. ``cash >= 0`` —— 任何时刻不得为负
      2. ``Σ持仓市值 + cash == total_asset``
      3. 买入后 cash 恰好减少 ``金额 + 费用``
    """

    account_id: str = "default"
    initial_capital: float = 10000.0
    cash: float = 10000.0
    positions: dict[str, Position] = field(default_factory=dict)
    pending_orders: list[Order] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[EquityPoint] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    last_trade_date: str = ""  # 已撮合到的交易日(防重复撮合)
    peak_asset: float = 0.0  # 历史最高总资产(算回撤用)

    def __post_init__(self) -> None:
        if self.peak_asset == 0.0:
            self.peak_asset = self.total_asset

    @property
    def market_value(self) -> float:
        """持仓总市值"""
        return sum(p.market_value for p in self.positions.values())

    @property
    def total_asset(self) -> float:
        """总资产 = 现金 + 持仓市值"""
        return self.cash + self.market_value

    @property
    def total_return(self) -> float:
        """累计收益率"""
        if self.initial_capital <= 0:
            return 0.0
        return (self.total_asset - self.initial_capital) / self.initial_capital

    @property
    def position_ratio(self) -> float:
        """仓位比例 = 持仓市值 / 总资产"""
        total = self.total_asset
        return self.market_value / total if total > 0 else 0.0

    def get_position(self, symbol: str) -> Position | None:
        """获取持仓(不存在返回 None)"""
        return self.positions.get(symbol)

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "initial_capital": self.initial_capital,
            "cash": self.cash,
            "positions": {k: v.to_dict() for k, v in self.positions.items()},
            "pending_orders": [o.to_dict() for o in self.pending_orders],
            "trades": [t.to_dict() for t in self.trades],
            "equity_curve": [e.to_dict() for e in self.equity_curve],
            "created_at": self.created_at,
            "last_trade_date": self.last_trade_date,
            "peak_asset": self.peak_asset,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Account:
        """从字典恢复(容忍未知字段, 便于版本演进)"""
        return cls(
            account_id=data.get("account_id", "default"),
            initial_capital=float(data.get("initial_capital", 10000.0)),
            cash=float(data.get("cash", 10000.0)),
            positions={k: Position.from_dict(v) for k, v in (data.get("positions") or {}).items()},
            pending_orders=[Order.from_dict(o) for o in (data.get("pending_orders") or [])],
            trades=[Trade.from_dict(t) for t in (data.get("trades") or [])],
            equity_curve=[EquityPoint.from_dict(e) for e in (data.get("equity_curve") or [])],
            created_at=data.get("created_at", ""),
            last_trade_date=data.get("last_trade_date", ""),
            peak_asset=float(data.get("peak_asset", 0.0)),
        )

    @classmethod
    def from_json(cls, text: str) -> Account:
        return cls.from_dict(json.loads(text))
