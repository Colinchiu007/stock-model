"""
策略引擎与回测引擎单元测试
"""

import numpy as np
import pandas as pd
import pytest

from stock_model.strategy.base import ActionType, BaseStrategy, StrategyResult
from stock_model.strategy.engine import (
    BacktestEngine,
    BacktestResult,
    StrategyEngine,
    StrategyPerformance,
    Trade,
)
from stock_model.strategy.manual import ManualStrategy
from stock_model.strategy.quant_strategy import QuantStrategy, StrategyParams


# --- 测试辅助 ---

def _make_test_df(days: int = 120, trend: str = "up") -> pd.DataFrame:
    """生成测试用行情数据"""
    np.random.seed(42)
    dates = pd.date_range("2024-01-01", periods=days, freq="D")

    if trend == "up":
        close = 10.0 + np.cumsum(np.random.randn(days) * 0.1 + 0.05)
    elif trend == "down":
        close = 20.0 + np.cumsum(np.random.randn(days) * 0.1 - 0.05)
    else:  # sideways
        close = 15.0 + np.random.randn(days) * 0.5

    close = np.maximum(close, 1.0)  # 确保价格为正

    df = pd.DataFrame(
        {
            "open": close * (1 + np.random.randn(days) * 0.01),
            "high": close * (1 + abs(np.random.randn(days) * 0.02)),
            "low": close * (1 - abs(np.random.randn(days) * 0.02)),
            "close": close,
            "volume": np.random.randint(10000, 100000, days).astype(float),
        },
        index=dates,
    )
    return df


class SimpleQuantStrategy(QuantStrategy):
    """简单量化策略(用于测试)"""

    name = "simple_quant"
    description = "简单量化测试策略"

    def analyze(self, symbol: str, df: pd.DataFrame) -> StrategyResult:
        if len(df) < 20:
            return StrategyResult(
                symbol=symbol, action=ActionType.HOLD, confidence=0.3, reason="数据不足"
            )

        ma5 = df["close"].rolling(5).mean().iloc[-1]
        ma20 = df["close"].rolling(20).mean().iloc[-1]
        current = df["close"].iloc[-1]

        if ma5 > ma20 and current > ma5:
            return StrategyResult(
                symbol=symbol,
                action=ActionType.BUY,
                confidence=0.7,
                reason="均线多头排列",
            )
        elif ma5 < ma20 and current < ma5:
            return StrategyResult(
                symbol=symbol,
                action=ActionType.SELL,
                confidence=0.7,
                reason="均线空头排列",
            )
        else:
            return StrategyResult(
                symbol=symbol,
                action=ActionType.HOLD,
                confidence=0.4,
                reason="均线纠缠",
            )


# --- Trade 测试 ---

class TestTrade:
    def test_trade_amount(self):
        trade = Trade(
            symbol="000001",
            action=ActionType.BUY,
            price=10.0,
            shares=100,
            timestamp=pd.Timestamp("2024-01-01"),
            commission=3.0,
        )
        assert trade.amount == 1003.0

    def test_trade_str(self):
        trade = Trade(
            symbol="000001",
            action=ActionType.BUY,
            price=10.0,
            shares=100,
            timestamp=pd.Timestamp("2024-01-01"),
        )
        assert "000001" in str(trade)
        assert "buy" in str(trade)


# --- BacktestResult 测试 ---

class TestBacktestResult:
    def test_total_return_positive(self):
        result = BacktestResult(
            strategy_name="test",
            symbol="000001",
            initial_cash=100000,
            final_cash=110000,
        )
        assert result.total_return == pytest.approx(0.1, abs=0.001)

    def test_total_return_negative(self):
        result = BacktestResult(
            strategy_name="test",
            symbol="000001",
            initial_cash=100000,
            final_cash=90000,
        )
        assert result.total_return == pytest.approx(-0.1, abs=0.001)

    def test_total_return_zero_initial(self):
        result = BacktestResult(
            strategy_name="test",
            symbol="000001",
            initial_cash=0,
            final_cash=0,
        )
        assert result.total_return == 0.0


# --- BacktestEngine 测试 ---

class TestBacktestEngine:
    def test_backtest_with_manual_strategy(self):
        df = _make_test_df(days=120, trend="up")
        strategy = ManualStrategy()
        engine = BacktestEngine(initial_cash=100000)
        result = engine.run(strategy, df, "000001")

        assert isinstance(result, BacktestResult)
        assert result.strategy_name == "manual"
        assert result.symbol == "000001"
        assert isinstance(result.metrics, dict)
        assert "total_return" in result.metrics

    def test_backtest_with_quant_strategy(self):
        df = _make_test_df(days=120, trend="up")
        strategy = SimpleQuantStrategy()
        engine = BacktestEngine(initial_cash=100000)
        result = engine.run(strategy, df, "000001")

        assert isinstance(result, BacktestResult)
        assert result.strategy_name == "simple_quant"
        assert len(result.trades) >= 0  # 可能有0笔交易

    def test_backtest_short_data(self):
        df = _make_test_df(days=5)
        strategy = ManualStrategy()
        engine = BacktestEngine(initial_cash=100000)
        result = engine.run(strategy, df, "000001")

        assert result.total_return == 0.0

    def test_backtest_metrics_keys(self):
        df = _make_test_df(days=120)
        strategy = ManualStrategy()
        engine = BacktestEngine(initial_cash=100000)
        result = engine.run(strategy, df, "000001")

        expected_keys = [
            "total_return", "annual_return", "sharpe", "sortino",
            "max_drawdown", "volatility",
        ]
        for key in expected_keys:
            assert key in result.metrics, f"缺少指标: {key}"

    def test_backtest_commission_and_slippage(self):
        df = _make_test_df(days=120, trend="up")
        strategy = ManualStrategy()

        # 高佣金
        engine_high = BacktestEngine(initial_cash=100000, commission_rate=0.01, slippage=0.01)
        result_high = engine_high.run(strategy, df, "000001")

        # 低佣金
        engine_low = BacktestEngine(initial_cash=100000, commission_rate=0.0001, slippage=0.0001)
        result_low = engine_low.run(strategy, df, "000001")

        # 高成本应该收益更低(或亏损更多)
        assert result_high.total_return <= result_low.total_return + 0.01  # 容差


# --- QuantStrategy 测试 ---

class TestQuantStrategy:
    def test_params_default(self):
        strategy = SimpleQuantStrategy()
        assert isinstance(strategy.params, StrategyParams)

    def test_set_get_param(self):
        strategy = SimpleQuantStrategy()
        strategy.set_param("custom_key", 42)
        assert strategy.get_param("custom_key") == 42
        assert strategy.get_param("nonexistent", "default") == "default"

    def test_params_to_dict(self):
        params = StrategyParams()
        d = params.to_dict()
        assert isinstance(d, dict)

    def test_optimize_basic(self):
        df = _make_test_df(days=120, trend="up")
        strategy = SimpleQuantStrategy()

        # 简单参数网格(不涉及实际策略参数，仅测试流程)
        param_grid = {}  # 空网格
        result = strategy.optimize(param_grid, df, "000001")
        assert isinstance(result, StrategyParams)

    def test_evaluate_uses_backtest(self):
        df = _make_test_df(days=120)
        strategy = SimpleQuantStrategy()
        metrics = strategy.evaluate("000001", df)

        assert isinstance(metrics, dict)
        assert "total_return" in metrics


# --- StrategyEngine 测试 ---

class TestStrategyEngine:
    def test_register_and_list(self):
        engine = StrategyEngine()
        strategy = ManualStrategy()
        engine.register(strategy, weight=1.0)

        assert "manual" in engine.list_strategies()

    def test_unregister(self):
        engine = StrategyEngine()
        strategy = ManualStrategy()
        engine.register(strategy)
        engine.unregister("manual")

        assert "manual" not in engine.list_strategies()

    def test_run_all(self):
        engine = StrategyEngine()
        engine.register(ManualStrategy(), weight=1.0)

        df = _make_test_df(days=120)
        results = engine.run_all("000001", df)

        assert "manual" in results
        assert isinstance(results["manual"], StrategyResult)

    def test_aggregate_signal_single(self):
        engine = StrategyEngine()
        engine.register(ManualStrategy(), weight=1.0)

        df = _make_test_df(days=120)
        results = engine.run_all("000001", df)
        aggregated = engine.aggregate_signal(results)

        assert isinstance(aggregated, StrategyResult)
        assert aggregated.action in [ActionType.BUY, ActionType.SELL, ActionType.HOLD]

    def test_aggregate_signal_empty(self):
        engine = StrategyEngine()
        aggregated = engine.aggregate_signal({})
        assert aggregated.action == ActionType.HOLD
        assert aggregated.confidence == 0.0

    def test_aggregate_signal_multiple(self):
        engine = StrategyEngine()
        engine.register(ManualStrategy(), weight=1.0)
        engine.register(SimpleQuantStrategy(), weight=0.5)

        df = _make_test_df(days=120)
        results = engine.run_all("000001", df)
        aggregated = engine.aggregate_signal(results)

        assert isinstance(aggregated, StrategyResult)
        assert "聚合信号" in aggregated.reason

    def test_get_performance(self):
        engine = StrategyEngine()
        engine.register(ManualStrategy(), weight=1.0)

        df = _make_test_df(days=120)
        engine.run_all("000001", df)

        perf = engine.get_performance()
        assert "manual" in perf
        assert perf["manual"].total_signals >= 1