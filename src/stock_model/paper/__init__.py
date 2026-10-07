"""模拟盘（Paper Trading）

用**真实时间线**验证策略表现，区别于回测的一次性历史回放。

设计要点见 ``docs/phase4_prd_paper_trading.md``。
"""

from stock_model.paper.broker import Broker, calc_fees, price_limit_pct
from stock_model.paper.engine import PaperEngine
from stock_model.paper.metrics import PerformanceMetrics, evaluate
from stock_model.paper.models import (
    Account,
    EquityPoint,
    Order,
    OrderStatus,
    Position,
    Side,
    Trade,
)

__all__ = [
    "Account",
    "Broker",
    "EquityPoint",
    "Order",
    "OrderStatus",
    "PaperEngine",
    "PerformanceMetrics",
    "Position",
    "Side",
    "Trade",
    "calc_fees",
    "evaluate",
    "price_limit_pct",
]
