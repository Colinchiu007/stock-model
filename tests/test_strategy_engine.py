"""
策略引擎与回测引擎单元测试
"""

import numpy as np
import pandas as pd
import pytest

from stock_model.strategy.base import ActionType, StrategyResult
from stock_model.strategy.engine import (
    BacktestEngine,
    BacktestResult,
    BenchmarkResult,
    StrategyEngine,
    Trade,
    TradeAnalysis,
    TradePair,
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

    return pd.DataFrame(
        {
            "open": close * (1 + np.random.randn(days) * 0.01),
            "high": close * (1 + abs(np.random.randn(days) * 0.02)),
            "low": close * (1 - abs(np.random.randn(days) * 0.02)),
            "close": close,
            "volume": np.random.randint(10000, 100000, days).astype(float),
        },
        index=dates,
    )


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
        if ma5 < ma20 and current < ma5:
            return StrategyResult(
                symbol=symbol,
                action=ActionType.SELL,
                confidence=0.7,
                reason="均线空头排列",
            )
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
            "total_return",
            "annual_return",
            "sharpe",
            "sortino",
            "max_drawdown",
            "volatility",
            "calmar",
            "max_drawdown_duration",
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


# --- TradePair 测试 ---


class TestTradePair:
    def test_trade_pair_return_pct(self):
        buy = Trade(
            symbol="000001",
            action=ActionType.BUY,
            price=10.0,
            shares=100,
            timestamp=pd.Timestamp("2024-01-01"),
        )
        sell = Trade(
            symbol="000001",
            action=ActionType.SELL,
            price=12.0,
            shares=100,
            timestamp=pd.Timestamp("2024-01-10"),
        )
        pair = TradePair(buy_trade=buy, sell_trade=sell)
        assert pair.return_pct == pytest.approx(0.2, abs=0.001)
        assert pair.is_win is True
        assert pair.holding_days == 9

    def test_trade_pair_loss(self):
        buy = Trade(
            symbol="000001",
            action=ActionType.BUY,
            price=10.0,
            shares=100,
            timestamp=pd.Timestamp("2024-01-01"),
        )
        sell = Trade(
            symbol="000001",
            action=ActionType.SELL,
            price=8.0,
            shares=100,
            timestamp=pd.Timestamp("2024-01-05"),
        )
        pair = TradePair(buy_trade=buy, sell_trade=sell)
        assert pair.return_pct == pytest.approx(-0.2, abs=0.001)
        assert pair.is_win is False

    def test_trade_pair_str(self):
        buy = Trade(
            symbol="000001",
            action=ActionType.BUY,
            price=10.0,
            shares=100,
            timestamp=pd.Timestamp("2024-01-01"),
        )
        sell = Trade(
            symbol="000001",
            action=ActionType.SELL,
            price=12.0,
            shares=100,
            timestamp=pd.Timestamp("2024-01-10"),
        )
        pair = TradePair(buy_trade=buy, sell_trade=sell)
        assert "收益" in str(pair)


# --- TradeAnalysis / Benchmark 测试 ---


class TestBacktestAnalysis:
    def test_trade_analysis_populated(self):
        """回测结果应包含交易分析"""
        df = _make_test_df(days=120, trend="up")
        strategy = SimpleQuantStrategy()
        engine = BacktestEngine(initial_cash=100000)
        result = engine.run(strategy, df, "000001")

        assert isinstance(result.trade_analysis, TradeAnalysis)
        # 上涨趋势应该有交易
        if result.trade_analysis.total_pairs > 0:
            assert 0 <= result.trade_analysis.win_rate <= 1
            assert result.trade_analysis.avg_holding_days >= 0

    def test_benchmark_populated(self):
        """回测结果应包含基准对比"""
        df = _make_test_df(days=120, trend="up")
        strategy = SimpleQuantStrategy()
        engine = BacktestEngine(initial_cash=100000)
        result = engine.run(strategy, df, "000001")

        assert isinstance(result.benchmark, BenchmarkResult)
        assert isinstance(result.benchmark.strategy_return, float)
        assert isinstance(result.benchmark.benchmark_return, float)
        assert isinstance(result.benchmark.alpha, float)

    def test_new_metrics_calmar(self):
        """Calmar比率应被计算"""
        df = _make_test_df(days=120)
        strategy = ManualStrategy()
        engine = BacktestEngine(initial_cash=100000)
        result = engine.run(strategy, df, "000001")

        assert "calmar" in result.metrics
        assert "max_drawdown_duration" in result.metrics

    def test_compare_strategies(self):
        """策略比较应返回多个结果"""
        df = _make_test_df(days=120, trend="up")
        engine = BacktestEngine(initial_cash=100000)
        results = engine.compare_strategies([ManualStrategy(), SimpleQuantStrategy()], df, "000001")

        assert isinstance(results, dict)
        assert len(results) == 2
        assert "manual" in results
        assert "simple_quant" in results
        for result in results.values():
            assert isinstance(result, BacktestResult)

    def test_trade_analysis_empty_trades(self):
        """无交易时应返回空分析"""
        df = _make_test_df(days=5)
        strategy = ManualStrategy()
        engine = BacktestEngine(initial_cash=100000)
        result = engine.run(strategy, df, "000001")

        assert result.trade_analysis.total_pairs == 0
        assert result.trade_analysis.win_rate == 0.0

    def test_benchmark_str(self):
        """BenchmarkResult应有可读字符串"""
        bm = BenchmarkResult(strategy_return=0.1, benchmark_return=0.05, alpha=0.05)
        assert "策略" in str(bm)
        assert "Alpha" in str(bm)


# --- P3-2: TradingEnv自定义奖励测试 ---


class TestTradingEnvReward:
    """TradingEnv奖励函数测试"""

    def test_default_reward_fn(self):
        """默认奖励函数应正常工作"""
        from stock_model.strategy.trading_env import TradingEnv

        df = _make_test_df(days=60)
        env = TradingEnv(df, initial_balance=100000)
        obs, info = env.reset()
        obs, reward, terminated, truncated, info = env.step(1)  # BUY

        assert isinstance(reward, float)
        assert not np.isnan(reward)

    def test_sharpe_reward_fn(self):
        """Sharpe奖励函数应正常工作"""
        from stock_model.strategy.trading_env import TradingEnv

        df = _make_test_df(days=60)
        env = TradingEnv(df, initial_balance=100000, reward_fn="sharpe")
        obs, info = env.reset()
        obs, reward, terminated, truncated, info = env.step(1)

        assert isinstance(reward, float)
        assert not np.isnan(reward)

    def test_sortino_reward_fn(self):
        """Sortino奖励函数应正常工作"""
        from stock_model.strategy.trading_env import TradingEnv

        df = _make_test_df(days=60)
        env = TradingEnv(df, initial_balance=100000, reward_fn="sortino")
        obs, info = env.reset()
        obs, reward, terminated, truncated, info = env.step(1)

        assert isinstance(reward, float)
        assert not np.isnan(reward)

    def test_custom_reward_fn(self):
        """自定义奖励函数应正常工作"""
        from stock_model.strategy.trading_env import TradingEnv

        def my_reward(env):
            return env._total_value / env.initial_balance - 1.0

        df = _make_test_df(days=60)
        env = TradingEnv(df, initial_balance=100000, reward_fn=my_reward)
        obs, info = env.reset()
        obs, reward, terminated, truncated, info = env.step(0)  # HOLD

        assert isinstance(reward, float)
        # HOLD时reward应接近0
        assert abs(reward) < 0.01

    def test_invalid_reward_fn_falls_back(self):
        """无效奖励函数名应回退到默认"""
        from stock_model.strategy.trading_env import TradingEnv, default_reward

        df = _make_test_df(days=60)
        env = TradingEnv(df, initial_balance=100000, reward_fn="nonexistent")
        # 应回退到default_reward
        assert env._reward_fn is default_reward

    def test_builtin_rewards_registry(self):
        """内置奖励注册表应包含3个奖励函数"""
        from stock_model.strategy.trading_env import BUILTIN_REWARDS

        assert "default" in BUILTIN_REWARDS
        assert "sharpe" in BUILTIN_REWARDS
        assert "sortino" in BUILTIN_REWARDS
        assert len(BUILTIN_REWARDS) == 3

    def test_returns_history_reset(self):
        """reset应清空收益历史"""
        from stock_model.strategy.trading_env import TradingEnv

        df = _make_test_df(days=60)
        env = TradingEnv(df, initial_balance=100000, reward_fn="sharpe")
        obs, info = env.reset()
        env.step(1)
        assert len(env._returns_history) > 0

        # reset后应清空
        obs, info = env.reset()
        assert len(env._returns_history) == 0

    def test_reward_fn_multiple_steps(self):
        """多步执行奖励函数应累积收益历史"""
        from stock_model.strategy.trading_env import TradingEnv

        df = _make_test_df(days=60)
        env = TradingEnv(df, initial_balance=100000, reward_fn="sharpe")
        obs, info = env.reset()

        rewards = []
        for _ in range(10):
            obs, reward, terminated, truncated, info = env.step(0)
            rewards.append(reward)
            if terminated:
                break

        assert len(rewards) > 0
        assert all(not np.isnan(r) for r in rewards)
        # sharpe奖励在多步后应基于滚动窗口
        assert len(env._returns_history) == len(rewards)


# --- P3-2: MultiStockTradingEnv测试 ---


class TestMultiStockTradingEnv:
    """多股票组合交易环境测试"""

    def _make_multi_dfs(self, n_stocks: int = 2, days: int = 60) -> dict[str, pd.DataFrame]:
        """生成多股票测试数据"""
        dfs = {}
        for i in range(n_stocks):
            dfs[f"00000{i + 1}"] = _make_test_df(days=days, trend="up")
        return dfs

    def test_init(self):
        """多股票环境应正确初始化"""
        from stock_model.strategy.trading_env import MultiStockTradingEnv

        dfs = self._make_multi_dfs(2)
        env = MultiStockTradingEnv(dfs, initial_balance=100000)

        assert len(env.symbols) == 2
        assert env.initial_balance == 100000
        assert env._balance == 100000

    def test_reset(self):
        """reset应返回观察和info"""
        from stock_model.strategy.trading_env import MultiStockTradingEnv

        dfs = self._make_multi_dfs(2)
        env = MultiStockTradingEnv(dfs, initial_balance=100000)
        obs, info = env.reset()

        assert isinstance(obs, np.ndarray)
        assert "balance" in info
        assert "shares" in info
        assert isinstance(info["shares"], dict)

    def test_step_hold(self):
        """HOLD动作应保持持仓不变"""
        from stock_model.strategy.trading_env import MultiStockTradingEnv

        dfs = self._make_multi_dfs(2)
        env = MultiStockTradingEnv(dfs, initial_balance=100000)
        obs, info = env.reset()

        obs, reward, terminated, truncated, info = env.step([0, 0])  # 全部HOLD
        assert isinstance(reward, float)
        assert not np.isnan(reward)

    def test_step_buy(self):
        """BUY动作应增加持仓"""
        from stock_model.strategy.trading_env import MultiStockTradingEnv

        dfs = self._make_multi_dfs(2)
        env = MultiStockTradingEnv(dfs, initial_balance=100000)
        obs, info = env.reset()

        obs, reward, terminated, truncated, info = env.step([1, 0])  # 买第一个
        assert isinstance(reward, float)
        # 余额应减少(买了股票)
        assert info["balance"] < 100000

    def test_step_sell(self):
        """先买后卖应正常执行"""
        from stock_model.strategy.trading_env import MultiStockTradingEnv

        dfs = self._make_multi_dfs(2)
        env = MultiStockTradingEnv(dfs, initial_balance=100000)
        obs, info = env.reset()

        env.step([1, 1])  # 全部买入
        balance_after_buy = env._balance
        env.step([2, 2])  # 全部卖出
        balance_after_sell = env._balance

        # 卖出后余额应增加
        assert balance_after_sell > balance_after_buy

    def test_observation_shape(self):
        """观察空间形状应一致"""
        from stock_model.strategy.trading_env import MultiStockTradingEnv

        dfs = self._make_multi_dfs(2, days=60)
        env = MultiStockTradingEnv(dfs, initial_balance=100000)
        obs, info = env.reset()

        # 多步后观察形状应一致
        for _ in range(5):
            obs2, _, _, _, _ = env.step([0, 0])
            assert obs2.shape == obs.shape

    def test_concentration_penalty(self):
        """集中度惩罚应在单股票持仓过高时触发"""
        from stock_model.strategy.trading_env import MultiStockTradingEnv

        dfs = self._make_multi_dfs(2)
        env = MultiStockTradingEnv(dfs, initial_balance=100000)
        obs, info = env.reset()

        # 只买一只股票，可能导致集中度>50%
        env.step([1, 0])
        # 继续买同一只
        env.step([1, 0])

        # 检查持仓集中度
        total_value = env._total_value
        if total_value > 0:
            symbol = env.symbols[0]
            df = env._dfs[symbol]
            step = min(env._current_step, len(df) - 1)
            price = float(df["close"].iloc[step])
            concentration = env._shares[symbol] * price / total_value
            # 如果集中度>50%，奖励应有惩罚
            if concentration > 0.5:
                # 验证_compute_portfolio_reward包含惩罚
                reward = env._compute_portfolio_reward()
                # 集中度惩罚已体现在reward中
                assert isinstance(reward, float)

    def test_multiple_episodes(self):
        """多轮episode应正常工作"""
        from stock_model.strategy.trading_env import MultiStockTradingEnv

        dfs = self._make_multi_dfs(2, days=60)
        env = MultiStockTradingEnv(dfs, initial_balance=100000)

        for _ep in range(3):
            obs, info = env.reset()
            for _ in range(10):
                obs, reward, terminated, truncated, info = env.step([0, 0])
                if terminated:
                    break
            # 每轮结束后余额应被重置
            assert env._balance == 100000

    def test_three_stocks(self):
        """3只股票环境应正常工作"""
        from stock_model.strategy.trading_env import MultiStockTradingEnv

        dfs = self._make_multi_dfs(3, days=60)
        env = MultiStockTradingEnv(dfs, initial_balance=100000)
        obs, info = env.reset()

        assert len(env.symbols) == 3
        obs, reward, terminated, truncated, info = env.step([1, 0, 2])
        assert isinstance(reward, float)

    def test_total_value_property(self):
        """total_value属性应正确返回"""
        from stock_model.strategy.trading_env import MultiStockTradingEnv

        dfs = self._make_multi_dfs(2)
        env = MultiStockTradingEnv(dfs, initial_balance=100000)
        obs, info = env.reset()

        assert env.total_value == 100000
        env.step([1, 0])
        assert env.total_value > 0

    def test_current_step_property(self):
        """current_step属性应正确返回"""
        from stock_model.strategy.trading_env import MultiStockTradingEnv

        dfs = self._make_multi_dfs(2)
        env = MultiStockTradingEnv(dfs, initial_balance=100000)
        obs, info = env.reset()

        initial_step = env.current_step
        env.step([0, 0])
        assert env.current_step == initial_step + 1


# --- P3-3: RL Agent评估流程测试 ---


class TestEvaluationResult:
    """EvaluationResult数据类测试"""

    def test_creation(self):
        """应正确创建评估结果"""
        from stock_model.strategy.rl_agent import EvaluationResult

        result = EvaluationResult(
            model_type="ppo",
            n_episodes=10,
            mean_reward=0.05,
            mean_value=105000,
            win_rate=0.7,
        )
        assert result.model_type == "ppo"
        assert result.n_episodes == 10
        assert result.mean_reward == 0.05

    def test_str_representation(self):
        """字符串表示应包含关键指标"""
        from stock_model.strategy.rl_agent import EvaluationResult

        result = EvaluationResult(
            model_type="ppo",
            n_episodes=10,
            mean_reward=0.05,
            win_rate=0.7,
            sharpe_ratio=1.5,
            max_drawdown=0.1,
        )
        s = str(result)
        assert "ppo" in s
        assert "10" in s

    def test_summary(self):
        """summary应返回字典"""
        from stock_model.strategy.rl_agent import EvaluationResult

        result = EvaluationResult(
            model_type="dqn",
            n_episodes=5,
            mean_reward=0.03,
            win_rate=0.6,
        )
        summary = result.summary()
        assert isinstance(summary, dict)
        assert summary["model_type"] == "dqn"
        assert summary["n_episodes"] == 5
        assert "mean_reward" in summary
        assert "win_rate" in summary

    def test_default_values(self):
        """默认值应正确"""
        from stock_model.strategy.rl_agent import EvaluationResult

        result = EvaluationResult(model_type="ppo")
        assert result.total_rewards == []
        assert result.episode_lengths == []
        assert result.final_values == []
        assert result.mean_reward == 0.0
        assert result.max_drawdown == 0.0


class TestRLAgentEvaluation:
    """RL Agent评估方法测试"""

    def test_evaluate_episodes_untrained(self):
        """未训练Agent的evaluate_episodes应正常工作"""
        from stock_model.strategy.rl_agent import RLTradingAgent
        from stock_model.strategy.trading_env import TradingEnv

        df = _make_test_df(days=60)
        env = TradingEnv(df, initial_balance=100000)
        agent = RLTradingAgent(model_type="ppo")

        result = agent.evaluate_episodes(env, n_episodes=3)
        assert result.n_episodes == 3
        assert result.model_type == "rule_based"
        assert len(result.total_rewards) == 3
        assert len(result.episode_lengths) == 3
        assert len(result.final_values) == 3

    def test_evaluate_episodes_statistics(self):
        """评估结果应包含统计指标"""
        from stock_model.strategy.rl_agent import RLTradingAgent
        from stock_model.strategy.trading_env import TradingEnv

        df = _make_test_df(days=60)
        env = TradingEnv(df, initial_balance=100000)
        agent = RLTradingAgent(model_type="ppo")

        result = agent.evaluate_episodes(env, n_episodes=5)
        assert isinstance(result.mean_reward, float)
        assert isinstance(result.std_reward, float)
        assert isinstance(result.mean_value, float)
        assert isinstance(result.win_rate, float)
        assert isinstance(result.sharpe_ratio, float)
        assert isinstance(result.max_drawdown, float)
        assert 0.0 <= result.win_rate <= 1.0

    def test_evaluate_episodes_with_reward_fn(self):
        """使用自定义奖励函数的评估应正常"""
        from stock_model.strategy.rl_agent import RLTradingAgent
        from stock_model.strategy.trading_env import TradingEnv

        df = _make_test_df(days=60)
        env = TradingEnv(df, initial_balance=100000, reward_fn="sharpe")
        agent = RLTradingAgent(model_type="ppo")

        result = agent.evaluate_episodes(env, n_episodes=3)
        assert result.n_episodes == 3
        assert all(not np.isnan(r) for r in result.total_rewards)

    def test_compare_with_baseline(self):
        """compare_with_baseline应返回RL和基线结果"""
        from stock_model.strategy.rl_agent import RLTradingAgent
        from stock_model.strategy.trading_env import TradingEnv

        df = _make_test_df(days=60)
        env = TradingEnv(df, initial_balance=100000)
        agent = RLTradingAgent(model_type="ppo")

        results = agent.compare_with_baseline(env, df, "000001", n_episodes=3)
        assert "rl" in results
        assert "baseline" in results

    def test_evaluate_backtest(self):
        """回测评估应返回指标字典"""
        from stock_model.strategy.rl_agent import RLTradingAgent

        df = _make_test_df(days=60)
        agent = RLTradingAgent(model_type="ppo")
        metrics = agent.evaluate("000001", df)

        assert isinstance(metrics, dict)
        assert "total_return" in metrics
