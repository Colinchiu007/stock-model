"""
风险数据模型

定义风险等级、风险类型、风险警报等数据结构。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class RiskLevel(str, Enum):
    """风险等级"""

    LOW = "low"           # 低风险
    WARNING = "warning"   # 警告
    DANGER = "danger"     # 危险
    CRITICAL = "critical" # 严重


class RiskType(str, Enum):
    """风险类型"""

    DRAWDOWN = "drawdown"              # 回撤风险
    CONCENTRATION = "concentration"    # 集中度风险
    VOLATILITY = "volatility"          # 波动率风险
    LIQUIDITY = "liquidity"            # 流动性风险
    STOP_LOSS = "stop_loss"            # 止损触发
    TAKE_PROFIT = "take_profit"        # 止盈触发
    POSITION_LIMIT = "position_limit"  # 仓位限制


@dataclass
class RiskAlert:
    """风险警报"""

    level: RiskLevel
    type: RiskType
    message: str
    action: str = ""           # 建议行动
    symbol: str = ""           # 相关股票代码
    value: float = 0.0         # 当前值
    threshold: float = 0.0     # 阈值
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return (
            f"RiskAlert({self.level.value}/{self.type.value}: "
            f"{self.message}, 建议={self.action})"
        )


@dataclass
class Position:
    """持仓信息"""

    symbol: str
    shares: int = 0
    cost_price: float = 0.0     # 成本价
    current_price: float = 0.0  # 当前价
    stop_loss: Optional[float] = None   # 止损价
    take_profit: Optional[float] = None # 止盈价

    @property
    def market_value(self) -> float:
        """市值"""
        return self.shares * self.current_price

    @property
    def cost_value(self) -> float:
        """成本市值"""
        return self.shares * self.cost_price

    @property
    def profit_loss(self) -> float:
        """盈亏金额"""
        return self.market_value - self.cost_value

    @property
    def profit_loss_pct(self) -> float:
        """盈亏比例"""
        if self.cost_value == 0:
            return 0.0
        return self.profit_loss / self.cost_value

    def __str__(self) -> str:
        return (
            f"Position({self.symbol}: {self.shares}股 @ {self.current_price:.2f}, "
            f"盈亏={self.profit_loss_pct:.2%})"
        )


@dataclass
class PortfolioRisk:
    """组合风险概览"""

    total_value: float = 0.0
    positions: List[Position] = field(default_factory=list)
    alerts: List[RiskAlert] = field(default_factory=list)

    @property
    def max_concentration(self) -> float:
        """最大单股集中度"""
        if self.total_value == 0:
            return 0.0
        return max(
            (p.market_value / self.total_value for p in self.positions if p.shares > 0),
            default=0.0,
        )

    @property
    def drawdown_pct(self) -> float:
        """当前回撤(需要外部计算)"""
        return 0.0

    @property
    def has_critical_alerts(self) -> bool:
        """是否有严重警报"""
        return any(a.level == RiskLevel.CRITICAL for a in self.alerts)