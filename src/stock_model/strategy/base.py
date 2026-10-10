"""
策略基类

所有策略继承此基类，统一接口。
第二期量化Agent将基于此接口扩展自动策略。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    import pandas as pd


class ActionType(str, Enum):
    """操作类型"""

    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


@dataclass
class StrategyResult:
    """策略执行结果"""

    symbol: str
    action: ActionType
    confidence: float  # 0.0 - 1.0
    reason: str = ""
    target_price: float | None = None
    stop_loss: float | None = None
    position_pct: float = 0.0  # 建议仓位比例 0-100
    metadata: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return (
            f"StrategyResult({self.symbol}: {self.action.value} | "
            f"信心度={self.confidence:.2f} | {self.reason})"
        )


class BaseStrategy(ABC):
    """策略基类"""

    name: str = "base"
    description: str = "基础策略"

    @abstractmethod
    def analyze(self, symbol: str, df: pd.DataFrame) -> StrategyResult:
        """
        分析股票并给出操作建议

        Args:
            symbol: 股票代码
            df: 行情数据

        Returns:
            策略执行结果
        """
        ...

    @abstractmethod
    def evaluate(self, symbol: str, df: pd.DataFrame) -> dict:
        """
        评估策略在历史数据上的表现

        Args:
            symbol: 股票代码
            df: 历史行情数据

        Returns:
            评估指标字典
        """
        ...

    def run(self, symbol: str, df: pd.DataFrame) -> StrategyResult:
        """
        执行策略

        Args:
            symbol: 股票代码
            df: 行情数据

        Returns:
            策略执行结果
        """
        logger.debug(f"执行策略 [{self.name}]: {symbol}")
        result = self.analyze(symbol, df)
        logger.debug(f"策略结果: {result}")
        return result
