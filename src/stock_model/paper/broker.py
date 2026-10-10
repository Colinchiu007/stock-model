"""撮合引擎

职责
----
把挂出的订单按 **T+1 开盘价** 撮合, 并施加 A 股交易规则。

**为什么必须是 T+1 开盘价**

信号在 T 日**收盘后**产生, 现实中最早只能在 T+1 成交。
若用 T 日收盘价成交即构成**未来函数** —— 会系统性高估策略表现。
现有 ``strategy/engine.py`` 的回测存在此问题, 模拟盘不得复制。

**施加的 A 股规则**

- T+1: 当日买入的股票当日不可卖出(通过 ``frozen_shares`` 实现)
- 最小交易单位: 买入必须 100 股整数倍
- 佣金: 万 3, 最低 5 元
- 印花税: 卖出 0.05%(2023-08 减半后), 买入不收
- 过户费: 0.001% 双向
- 涨跌停: 主板 ±10%, 创业板/科创板 ±20%, 触及则当日不撮合
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from loguru import logger

from stock_model.paper.models import Account, Order, OrderStatus, Position, Side, Trade

if TYPE_CHECKING:
    import pandas as pd

# 费用参数(行业惯例, 均可配置)
COMMISSION_RATE = 0.0003  # 佣金 万3
COMMISSION_MIN = 5.0  # 佣金最低 5 元
STAMP_TAX_RATE = 0.0005  # 印花税 0.05%, 仅卖出
TRANSFER_FEE_RATE = 0.00001  # 过户费 0.001%, 双向
LOT_SIZE = 100  # 最小交易单位

# 涨跌停幅度
LIMIT_20PCT_CODES = ("300", "301", "688", "689")


def price_limit_pct(symbol: str) -> float:
    """按代码判定涨跌停幅度

    创业板(300/301)与科创板(688/689)为 ±20%, 其余(主板/北交所)为 ±10%。

    注意: 用 lstrip("szsh") 会误删同名前缀字符(数字代码开头不受影响,
    但形如 "sz300750" 之外的输入会被破坏), 因此只剥离已知前缀。
    """
    code = symbol[-6:] if len(symbol) >= 6 else symbol
    for prefix in ("sz", "sh", "bj"):
        if code.lower().startswith(prefix):
            code = code[len(prefix) :]
            break
    for prefix in LIMIT_20PCT_CODES:
        if code.startswith(prefix):
            return 0.20
    return 0.10


def is_limit_up(bar: pd.Series) -> bool:
    """是否涨停(触及涨幅上限, 当日难以买入)"""
    pct = bar.get("pct_change")
    if pct is None or (isinstance(pct, float) and math.isnan(pct)):
        return False
    # bool() 是显式收口: pandas 无 stub, pct 是 Any, 直接返回会让
    # `warn_return_any` 报「Returning Any from function declared to return bool」
    return bool(pct >= price_limit_pct(str(bar.get("symbol", ""))) * 100 - 0.3)


def is_limit_down(bar: pd.Series) -> bool:
    """是否跌停(触及跌幅下限, 当日难以卖出)"""
    pct = bar.get("pct_change")
    if pct is None or (isinstance(pct, float) and math.isnan(pct)):
        return False
    return bool(pct <= -price_limit_pct(str(bar.get("symbol", ""))) * 100 + 0.3)


def is_suspended(bar: pd.Series) -> bool:
    """是否停牌(无成交量)"""
    vol = bar.get("volume")
    return vol is None or (isinstance(vol, float) and math.isnan(vol)) or vol <= 0


@dataclass
class FeeBreakdown:
    """费用明细"""

    commission: float
    stamp_tax: float
    transfer_fee: float

    @property
    def total(self) -> float:
        return self.commission + self.stamp_tax + self.transfer_fee


def calc_fees(side: Side, amount: float) -> FeeBreakdown:
    """计算费用

    Args:
        side: 买卖方向
        amount: 成交金额

    Returns:
        费用明细
    """
    commission = max(amount * COMMISSION_RATE, COMMISSION_MIN)
    transfer = amount * TRANSFER_FEE_RATE
    stamp = amount * STAMP_TAX_RATE if side == Side.SELL else 0.0
    return FeeBreakdown(
        commission=round(commission, 4),
        stamp_tax=round(stamp, 4),
        transfer_fee=round(transfer, 4),
    )


class Broker:
    """撮合引擎

    典型用法::

        broker = Broker(account)
        # T 日收盘后: 策略信号 -> 挂单
        broker.submit_order(order)
        # T+1: 用当日 bar 撮合
        broker.match(bar, trade_date)
    """

    def __init__(
        self,
        account: Account,
        max_position_pct: float = 0.20,
    ) -> None:
        self.account = account
        self.max_position_pct = max_position_pct
        self._trade_seq = 0

    # ==================== 订单管理 ====================

    def submit_order(self, order: Order) -> None:
        """挂单(不立即成交, 等待 T+1 撮合)"""
        order.status = OrderStatus.PENDING
        self.account.pending_orders.append(order)
        logger.debug(
            f"挂单: {order.side.value} {order.symbol} x{order.shares} (信号日 {order.created_date})"
        )

    def cancel_pending(self, reason: str = "手动取消") -> int:
        """取消所有待成交订单, 返回取消数量"""
        count = 0
        for order in self.account.pending_orders:
            if order.status == OrderStatus.PENDING:
                order.status = OrderStatus.CANCELLED
                order.reject_reason = reason
                count += 1
        self.account.pending_orders = [
            o for o in self.account.pending_orders if o.status == OrderStatus.PENDING
        ]
        return count

    # ==================== 撮合 ====================

    def match(self, bar: pd.Series, trade_date: str) -> list[Trade]:
        """用 T+1 的行情 bar 撮合当日待成交订单

        Args:
            bar: 当日行情(需含 open/volume/pct_change/symbol)
            trade_date: 交易日(ISO)

        Returns:
            本次产生的成交记录
        """
        fills: list[Trade] = []
        for order in list(self.account.pending_orders):
            if order.status != OrderStatus.PENDING:
                continue
            trade = self._match_one(order, bar, trade_date)
            if trade is not None:
                fills.append(trade)

        # 清理已处理的订单
        self.account.pending_orders = [
            o for o in self.account.pending_orders if o.status == OrderStatus.PENDING
        ]
        return fills

    def _match_one(self, order: Order, bar: pd.Series, trade_date: str) -> Trade | None:
        """撮合单个订单"""
        if is_suspended(bar):
            self._reject(order, f"{trade_date} 停牌, 订单顺延")
            return None

        price = float(bar["open"])
        if price <= 0:
            self._reject(order, f"{trade_date} 开盘价无效({price})")
            return None

        if order.side == Side.BUY:
            if is_limit_up(bar):
                self._reject(order, f"{trade_date} 涨停, 买单未成交")
                return None
            return self._fill_buy(order, price, bar, trade_date)
        return self._fill_sell(order, price, bar, trade_date)

    def _fill_buy(  # noqa: PLR0911
        self, order: Order, price: float, bar: pd.Series, trade_date: str
    ) -> Trade | None:
        """撮合买入

        多个早退分支(整百/资金/集中度)各对应一条不同的拒单原因,
        拆开会牺牲可读性, 故豁免 return 数量限制。
        """
        # 1. 整百
        shares = int(order.shares / LOT_SIZE) * LOT_SIZE
        if shares < LOT_SIZE:
            self._reject(order, f"不足 {LOT_SIZE} 股, 拒单")
            return None

        # 2. 现金约束(含费用): 逐档缩量直到能买得起
        while shares >= LOT_SIZE:
            amount = price * shares
            fees = calc_fees(Side.BUY, amount)
            if amount + fees.total <= self.account.cash:
                break
            shares -= LOT_SIZE
        else:
            self._reject(
                order, f"资金不足(可用 {self.account.cash:.2f}, 需 {price * LOT_SIZE:.2f}+费用)"
            )
            return None

        if shares < LOT_SIZE:
            self._reject(order, f"资金不足(可用 {self.account.cash:.2f})")
            return None

        amount = price * shares
        fees = calc_fees(Side.BUY, amount)

        # 3. 集中度约束
        #    注意: 必须对「首次买入」同样生效 —— 此处若写成 `if pos is not None`
        #    则新建仓时 pos 为 None 而整段被跳过, 约束完全失效(实测 90% 未被拦)。
        #
        #    占比须按**成交后**的真实结构计算: 买入会同时减少现金、增加市值,
        #    所以分母是 (现金 - 本次支出) + (原市值 + 本次金额), 而非原总资产。
        pos = self.account.get_position(order.symbol)
        held_mv = pos.market_value if pos is not None else 0.0
        cash_after = self.account.cash - amount - fees.total
        total_after = cash_after + held_mv + amount
        if total_after > 0:
            mv_after = held_mv + amount
            if mv_after / total_after > self.max_position_pct:
                # 反解允许的最大金额: mv / (cash - mv - fee + mv) <= pct
                # 保守起见忽略 fee, 解 mv <= pct * cash / (1 + pct)
                allowed_mv = self.max_position_pct * self.account.cash / (1 + self.max_position_pct)
                extra_allowed = max(0.0, allowed_mv - held_mv)
                max_shares = int(extra_allowed / price / LOT_SIZE) * LOT_SIZE
                if max_shares < LOT_SIZE:
                    self._reject(order, f"超单票集中度上限({self.max_position_pct:.0%})")
                    return None
                # 缩量后需重新校验现金充足
                amount = price * max_shares
                new_fees = calc_fees(Side.BUY, amount)
                while max_shares >= LOT_SIZE and (amount + new_fees.total > self.account.cash):
                    max_shares -= LOT_SIZE
                    amount = price * max_shares
                    new_fees = calc_fees(Side.BUY, amount)
                if max_shares < LOT_SIZE:
                    self._reject(order, f"资金不足(可用 {self.account.cash:.2f})")
                    return None
                shares = max_shares
                amount = price * shares
                fees = new_fees

        # 4. 扣款
        self.account.cash -= amount + fees.total
        if self.account.cash < 0:  # pragma: no cover - 双保险
            self._reject(order, "内部错误: 现金将为负")
            return None

        # 5. 更新持仓(移动加权平均)
        #    注意: 新建时 avg_cost 必须为 0, 统一由 _recalc_avg_cost 计算。
        #    若此处预设为 price, 重算时 old_shares=0 但 avg_cost 非 0,
        #    会得出错误的加权成本。
        if pos is None:
            pos = Position(symbol=order.symbol, avg_cost=0.0, open_date=trade_date)
            self.account.positions[order.symbol] = pos
        pos.shares += shares
        pos.avg_cost = self._recalc_avg_cost(order.symbol, price, shares)
        pos.last_price = price
        pos.frozen_shares += shares  # T+1: 当日买入冻结
        pos.open_date = pos.open_date or trade_date

        trade = self._record_trade(order, Side.BUY, shares, price, amount, fees, trade_date)
        order.status = OrderStatus.FILLED
        order.trade_id = trade.trade_id
        logger.info(
            f"[模拟成交] 买入 {order.symbol} {shares}股 @{price:.2f} "
            f"费用{fees.total:.2f} 余额{self.account.cash:.2f}"
        )
        return trade

    def _fill_sell(
        self, order: Order, price: float, bar: pd.Series, trade_date: str
    ) -> Trade | None:
        """撮合卖出"""
        if is_limit_down(bar):
            self._reject(order, f"{trade_date} 跌停, 卖单未成交")
            return None

        pos = self.account.get_position(order.symbol)
        if pos is None or pos.shares <= 0:
            self._reject(order, "无持仓可卖")
            return None

        # T+1: 只能卖可卖部分
        shares = min(order.shares, pos.available_shares)
        if shares < LOT_SIZE:
            self._reject(order, f"T+1 限制: 当日买入不可卖出(可卖 {pos.available_shares} 股)")
            return None

        amount = price * shares
        fees = calc_fees(Side.SELL, amount)

        self.account.cash += amount - fees.total
        pos.shares -= shares
        pos.frozen_shares = max(0, pos.frozen_shares - shares)
        pos.last_price = price
        if pos.shares <= 0:
            del self.account.positions[order.symbol]

        trade = self._record_trade(order, Side.SELL, shares, price, amount, fees, trade_date)
        order.status = OrderStatus.FILLED
        order.trade_id = trade.trade_id
        logger.info(
            f"[模拟成交] 卖出 {order.symbol} {shares}股 @{price:.2f} "
            f"费用{fees.total:.2f} 余额{self.account.cash:.2f}"
        )
        return trade

    def _recalc_avg_cost(self, symbol: str, price: float, shares: int) -> float:
        """重算移动加权平均成本

        新均价 = (原持仓成本 + 本次买入金额) / (原股数 + 本次股数)
        其中原持仓成本须用**买入前**的股数计算, 故此处 pos.shares 已含本次。
        """
        pos = self.account.positions[symbol]
        old_shares = pos.shares - shares
        old_cost = pos.avg_cost * old_shares
        return round((old_cost + price * shares) / pos.shares, 4) if pos.shares else 0.0

    def _record_trade(
        self,
        order: Order,
        side: Side,
        shares: int,
        price: float,
        amount: float,
        fees: FeeBreakdown,
        trade_date: str,
    ) -> Trade:
        """落一条成交记录"""
        self._trade_seq += 1
        trade = Trade(
            trade_id=f"T{self._trade_seq:06d}",
            symbol=order.symbol,
            side=side,
            shares=shares,
            price=round(price, 4),
            amount=round(amount, 4),
            commission=fees.commission,
            stamp_tax=fees.stamp_tax,
            transfer_fee=fees.transfer_fee,
            signal_source=order.signal_action,
            signal_confidence=order.signal_confidence,
            executed_at=trade_date,
        )
        self.account.trades.append(trade)
        return trade

    def _reject(self, order: Order, reason: str) -> None:
        """拒单(记录原因, 从待撮合队列移除)"""
        order.status = OrderStatus.REJECTED
        order.reject_reason = reason
        logger.debug(f"[模拟拒单] {order.side.value} {order.symbol}: {reason}")

    # ==================== 估值 ====================

    def mark_to_market(self, prices: dict[str, float], date: str) -> float:
        """按最新价重估持仓并记录资金曲线

        Args:
            prices: {symbol: price}
            date: 交易日(ISO)

        Returns:
            更新后的总资产
        """
        from stock_model.paper.models import EquityPoint

        for symbol, pos in self.account.positions.items():
            px = prices.get(symbol)
            if px is not None and px > 0:
                pos.last_price = px

        # 解冻: 隔夜后冻结股数清零
        for pos in self.account.positions.values():
            pos.frozen_shares = 0

        total = self.account.total_asset
        self.account.peak_asset = max(self.account.peak_asset, total)
        drawdown = (
            (total - self.account.peak_asset) / self.account.peak_asset
            if self.account.peak_asset > 0
            else 0.0
        )

        self.account.equity_curve.append(
            EquityPoint(
                date=date,
                total_asset=round(total, 2),
                cash=round(self.account.cash, 2),
                market_value=round(self.account.market_value, 2),
                drawdown=round(drawdown, 4),
            )
        )
        self.account.last_trade_date = date
        return total

    def unfreeze_all(self) -> None:
        """解冻所有持仓(新交易日开始时调用)"""
        for pos in self.account.positions.values():
            pos.frozen_shares = 0
