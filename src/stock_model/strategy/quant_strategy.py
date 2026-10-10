"""
量化策略基类

支持参数化策略和参数优化，继承BaseStrategy统一接口。
第二期核心模块，为自动量化Agent提供策略框架。
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np
from loguru import logger

from stock_model.strategy.base import BaseStrategy, StrategyResult

if TYPE_CHECKING:
    import pandas as pd


@dataclass
class StrategyParams:
    """策略参数基类

    子类应定义具体策略参数字段。
    """

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {k: v for k, v in vars(self).items() if not k.startswith("_")}

    @classmethod
    def from_dict(cls, params: dict[str, Any]) -> StrategyParams:
        """从字典创建"""
        return cls(**params)


class QuantStrategy(BaseStrategy):
    """量化策略基类

    继承BaseStrategy，增加参数化支持和参数优化能力。

    使用示例:
        class MyStrategy(QuantStrategy):
            params_class = MyParams

            def analyze(self, symbol, df):
                # 使用 self.params 访问参数
                ...
    """

    params_class: type = StrategyParams

    def __init__(self, params: StrategyParams | None = None):
        self._params = params or self.params_class()

    @property
    def params(self) -> StrategyParams:
        """当前策略参数"""
        return self._params

    @params.setter
    def params(self, value: StrategyParams) -> None:
        self._params = value

    def set_param(self, key: str, value: Any) -> None:
        """设置单个参数"""
        setattr(self._params, key, value)

    def get_param(self, key: str, default: Any = None) -> Any:
        """获取单个参数"""
        return getattr(self._params, key, default)

    def optimize(
        self,
        param_grid: dict[str, list[Any]],
        df: pd.DataFrame,
        symbol: str = "test",
        metric: str = "total_return",
    ) -> StrategyParams:
        """网格搜索参数优化

        Args:
            param_grid: 参数网格，如 {"fast_period": [5, 10, 20], "slow_period": [20, 40, 60]}
            df: 历史数据
            symbol: 股票代码
            metric: 优化目标指标，如 "total_return", "sharpe", "win_rate"

        Returns:
            最优参数
        """
        from stock_model.strategy.engine import BacktestEngine

        best_params = None
        best_score = -np.inf

        # 生成参数组合
        param_combos = self._generate_combinations(param_grid)
        total = len(param_combos)
        logger.info(f"参数优化: {total} 组合待评估, 目标指标={metric}")

        for i, combo in enumerate(param_combos):
            # 更新参数
            for key, value in combo.items():
                self.set_param(key, value)

            try:
                # 回测评估
                engine = BacktestEngine(initial_cash=100000)
                result = engine.run(self, df, symbol)
                score = result.metrics.get(metric, -np.inf)

                if score > best_score:
                    best_score = score
                    best_params = combo.copy()
                    logger.debug(f"  [{i + 1}/{total}] 新最优: {combo}, {metric}={score:.4f}")

            except (ValueError, KeyError, TypeError) as e:
                logger.warning(f"  [{i + 1}/{total}] 参数组合 {combo} 回测失败: {e}")
                continue

        if best_params:
            for key, value in best_params.items():
                self.set_param(key, value)
            logger.info(f"参数优化完成: 最优参数={best_params}, {metric}={best_score:.4f}")
        else:
            logger.warning("参数优化完成: 未找到有效参数组合")

        return self._params

    @staticmethod
    def _generate_combinations(param_grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
        """生成参数组合（笛卡尔积）"""
        keys = list(param_grid.keys())
        values = list(param_grid.values())

        combos = []
        for combo_values in np.ndindex(*[len(v) for v in values]):
            combo = {}
            for key, idx in zip(keys, combo_values, strict=False):
                combo[key] = param_grid[key][idx]
            combos.append(combo)

        return combos

    @abstractmethod
    def analyze(self, symbol: str, df: pd.DataFrame) -> StrategyResult:
        """分析股票并给出操作建议（子类必须实现）"""
        ...

    def evaluate(self, symbol: str, df: pd.DataFrame) -> dict:
        """评估策略在历史数据上的表现"""
        from stock_model.strategy.engine import BacktestEngine

        engine = BacktestEngine(initial_cash=100000)
        result = engine.run(self, df, symbol)
        return result.metrics
