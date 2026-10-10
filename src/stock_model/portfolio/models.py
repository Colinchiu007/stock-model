"""
组合数据模型
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass
class PortfolioWeight:
    """组合权重"""

    symbol: str
    weight: float  # 0-1
    shares: int = 0
    price: float = 0.0

    @property
    def value(self) -> float:
        """市值"""
        return self.shares * self.price

    def __str__(self) -> str:
        return f"PortfolioWeight({self.symbol}: weight={self.weight:.2%}, shares={self.shares})"


@dataclass
class Portfolio:
    """投资组合"""

    name: str = "default"
    total_value: float = 0.0
    weights: list[PortfolioWeight] = field(default_factory=list)
    # 混合类型: method(str) / n_assets(int) / volatilities(dict)。
    # 原注解 dict[str, float] 与实现不符(5 处 dict-item 报错), 是注解在说谎。
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def symbols(self) -> list[str]:
        """组合中的股票代码"""
        return [w.symbol for w in self.weights]

    @property
    def weight_vector(self) -> np.ndarray:
        """权重向量"""
        return np.array([w.weight for w in self.weights])

    @property
    def is_valid(self) -> bool:
        """权重是否有效(和为1)"""
        if not self.weights:
            return False
        total = sum(w.weight for w in self.weights)
        return abs(total - 1.0) < 0.01

    def __str__(self) -> str:
        weight_str = ", ".join(f"{w.symbol}={w.weight:.2%}" for w in self.weights)
        return f"Portfolio({self.name}: {weight_str}, value={self.total_value:.0f})"
