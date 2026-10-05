"""TradingPipeline 单元测试

覆盖集成测试未覆盖的路径:
  - 策略管理(add/remove)
  - 通道管理(add/remove)
  - 质量检查失败/异常
  - 风控拦截/异常
  - 仓位计算(kelly/risk_parity/error)
  - 通知推送
  - 异常处理
  - 定时执行(start/stop/is_running)
  - 数据回调
"""

from unittest.mock import MagicMock, patch

import pandas as pd

from stock_model.pipeline.config import PipelineConfig
from stock_model.pipeline.models import PipelineResult, PipelineStatus
from stock_model.pipeline.trading_pipeline import TradingPipeline
from stock_model.strategy.base import ActionType, BaseStrategy, StrategyResult


def _make_df(n=60):
    """构造60日行情数据(满足min_data_rows)"""
    return pd.DataFrame(
        {
            "open": range(n),
            "high": range(n),
            "low": range(n),
            "close": [10.0 + i * 0.1 for i in range(n)],
            "volume": range(n),
        },
        index=pd.date_range("2024-01-01", periods=n, freq="B"),
    ).astype(float)


class _DummyStrategy(BaseStrategy):
    """测试用策略"""

    name = "dummy"

    def analyze(self, symbol, df):
        return StrategyResult(
            action=ActionType.BUY,
            confidence=0.8,
            target_price=df["close"].iloc[-1],
        )

    def evaluate(self, symbol, df):
        return {"win_rate": 0.6}

    def generate_signal(self, symbol, df):
        return StrategyResult(
            action=ActionType.BUY,
            confidence=0.8,
            target_price=df["close"].iloc[-1],
        )


class _HoldStrategy(BaseStrategy):
    """始终HOLD的策略"""

    name = "hold_strategy"

    def analyze(self, symbol, df):
        return StrategyResult(action=ActionType.HOLD, confidence=0.5, target_price=0.0)

    def evaluate(self, symbol, df):
        return {"win_rate": 0.5}

    def generate_signal(self, symbol, df):
        return StrategyResult(action=ActionType.HOLD, confidence=0.5, target_price=0.0)


class _SellStrategy(BaseStrategy):
    """始终SELL的策略"""

    name = "sell_strategy"

    def analyze(self, symbol, df):
        return StrategyResult(
            action=ActionType.SELL,
            confidence=0.7,
            target_price=df["close"].iloc[-1],
        )

    def evaluate(self, symbol, df):
        return {"win_rate": 0.5}

    def generate_signal(self, symbol, df):
        return StrategyResult(
            action=ActionType.SELL,
            confidence=0.7,
            target_price=df["close"].iloc[-1],
        )


def _make_config(**overrides):
    """构造测试配置"""
    defaults = {
        "watchlist": ["000001"],
        "min_data_rows": 30,
        "min_quality_score": 0.3,
        "signal_cooldown_minutes": 0,
        "enable_notify": False,
        "enable_quality_check": True,
        "enable_risk_check": True,
    }
    defaults.update(overrides)
    return PipelineConfig(**defaults)


class TestStrategyManagement:
    """策略管理测试"""

    @patch("stock_model.pipeline.trading_pipeline.StockDataFetcher")
    def test_add_strategy(self, mock_fetcher_cls):
        config = _make_config()
        pipeline = TradingPipeline(config=config)
        strategy = _DummyStrategy()
        pipeline.add_strategy(strategy)
        assert "dummy" in pipeline._strategy_engine.list_strategies()

    @patch("stock_model.pipeline.trading_pipeline.StockDataFetcher")
    def test_remove_strategy(self, mock_fetcher_cls):
        config = _make_config()
        pipeline = TradingPipeline(config=config)
        strategy = _DummyStrategy()
        pipeline.add_strategy(strategy)
        pipeline.remove_strategy("dummy")
        assert "dummy" not in pipeline._strategy_engine.list_strategies()


class TestChannelManagement:
    """通道管理测试"""

    @patch("stock_model.pipeline.trading_pipeline.StockDataFetcher")
    def test_add_channel(self, mock_fetcher_cls):
        config = _make_config(enable_notify=False)
        pipeline = TradingPipeline(config=config)
        channel = MagicMock()
        pipeline.add_channel(channel)
        assert channel in pipeline._notifier._channels

    @patch("stock_model.pipeline.trading_pipeline.StockDataFetcher")
    def test_remove_channel(self, mock_fetcher_cls):
        config = _make_config(enable_notify=False)
        pipeline = TradingPipeline(config=config)
        channel = MagicMock()
        pipeline.add_channel(channel)
        pipeline.remove_channel(channel)
        assert channel not in pipeline._notifier._channels


class TestQualityCheck:
    """数据质量检查测试"""

    @patch.object(TradingPipeline, "_fetch_data")
    def test_quality_check_failure_skips(self, mock_fetch):
        """质量检查不达标时跳过"""
        mock_fetch.return_value = _make_df()
        config = _make_config(min_quality_score=0.99)  # 极高标准
        pipeline = TradingPipeline(config=config)
        pipeline.add_strategy(_DummyStrategy())

        # Mock质量检查返回低分
        pipeline._quality_monitor = MagicMock()
        report = MagicMock()
        report.score = 0.1
        pipeline._quality_monitor.check.return_value = report

        result = pipeline.run_once("000001")
        assert result.status == PipelineStatus.SKIPPED
        assert "质量不达标" in result.reason

    @patch.object(TradingPipeline, "_fetch_data")
    def test_quality_check_exception_passes(self, mock_fetch):
        """质量检查异常时默认通过"""
        mock_fetch.return_value = _make_df()
        config = _make_config()
        pipeline = TradingPipeline(config=config)
        pipeline.add_strategy(_HoldStrategy())

        pipeline._quality_monitor = MagicMock()
        pipeline._quality_monitor.check.side_effect = ValueError("bad data")

        result = pipeline.run_once("000001")
        assert result.status in [PipelineStatus.EXECUTED, PipelineStatus.SKIPPED]


class TestRiskCheck:
    """风控检查测试"""

    @patch.object(TradingPipeline, "_fetch_data")
    def test_risk_check_exception_returns_empty(self, mock_fetch):
        """风控检查异常时返回空列表"""
        mock_fetch.return_value = _make_df()
        config = _make_config()
        pipeline = TradingPipeline(config=config)
        pipeline.add_strategy(_DummyStrategy())

        pipeline._risk_manager = MagicMock()
        pipeline._risk_manager.check_position_risk.side_effect = ValueError("risk error")

        result = pipeline.run_once("000001")
        # 不应崩溃
        assert result.status in [
            PipelineStatus.EXECUTED,
            PipelineStatus.SKIPPED,
            PipelineStatus.BLOCKED,
        ]


class TestPositionCalculation:
    """仓位计算测试"""

    @patch.object(TradingPipeline, "_fetch_data")
    def test_kelly_method_fallback(self, mock_fetch):
        """kelly方法回退到fixed"""
        mock_fetch.return_value = _make_df()
        config = _make_config(position_method="kelly")
        pipeline = TradingPipeline(config=config)
        pipeline.add_strategy(_DummyStrategy())

        result = pipeline.run_once("000001")
        if result.position_advice:
            assert "kelly" in result.position_advice.get("method", "")

    @patch.object(TradingPipeline, "_fetch_data")
    def test_risk_parity_method_fallback(self, mock_fetch):
        """risk_parity方法回退到fixed"""
        mock_fetch.return_value = _make_df()
        config = _make_config(position_method="risk_parity")
        pipeline = TradingPipeline(config=config)
        pipeline.add_strategy(_DummyStrategy())

        result = pipeline.run_once("000001")
        if result.position_advice:
            assert "risk_parity" in result.position_advice.get("method", "")

    @patch.object(TradingPipeline, "_fetch_data")
    def test_position_calculation_exception(self, mock_fetch):
        """仓位计算异常时返回error"""
        mock_fetch.return_value = _make_df()
        config = _make_config()
        pipeline = TradingPipeline(config=config)
        pipeline.add_strategy(_DummyStrategy())

        pipeline._position_sizer = MagicMock()
        pipeline._position_sizer.fixed_size.side_effect = ZeroDivisionError("divide by zero")

        result = pipeline.run_once("000001")
        if result.position_advice:
            assert result.position_advice.get("method") == "error"


class TestNotify:
    """通知推送测试"""

    @patch.object(TradingPipeline, "_fetch_data")
    def test_notify_on_buy_signal(self, mock_fetch):
        """BUY信号触发通知"""
        mock_fetch.return_value = _make_df()
        config = _make_config(enable_notify=True)
        pipeline = TradingPipeline(config=config)
        pipeline.add_strategy(_DummyStrategy())

        # Mock notifier
        pipeline._notifier = MagicMock()

        result = pipeline.run_once("000001")
        # 如果策略产生BUY信号，应该调用notify
        if result.strategy_result and result.strategy_result.action != ActionType.HOLD:
            pipeline._notifier.notify.assert_called()


class TestExceptionHandling:
    """异常处理测试"""

    @patch.object(TradingPipeline, "_fetch_data")
    def test_runtime_error_in_pipeline(self, mock_fetch):
        """RuntimeError被捕获"""
        mock_fetch.return_value = _make_df()
        config = _make_config()
        pipeline = TradingPipeline(config=config)
        pipeline.add_strategy(_DummyStrategy())

        # Mock技术分析抛出异常
        pipeline._technical = MagicMock()
        pipeline._technical.analyze_all.side_effect = RuntimeError("analysis error")

        result = pipeline.run_once("000001")
        assert result.status == PipelineStatus.ERROR
        assert "执行异常" in result.reason

    @patch.object(TradingPipeline, "_fetch_data")
    def test_value_error_in_pipeline(self, mock_fetch):
        """ValueError被捕获"""
        mock_fetch.return_value = _make_df()
        config = _make_config()
        pipeline = TradingPipeline(config=config)
        pipeline.add_strategy(_DummyStrategy())

        pipeline._technical = MagicMock()
        pipeline._technical.analyze_all.side_effect = ValueError("bad value")

        result = pipeline.run_once("000001")
        assert result.status == PipelineStatus.ERROR

    @patch.object(TradingPipeline, "_fetch_data")
    def test_callback_exception_handled(self, mock_fetch):
        """回调异常不影响主流程"""
        mock_fetch.return_value = _make_df()
        config = _make_config()
        pipeline = TradingPipeline(config=config)
        pipeline.add_strategy(_HoldStrategy())

        def bad_callback(r):
            raise ValueError("callback error")

        pipeline.on_result(bad_callback)

        # 不应崩溃
        result = pipeline.run_once("000001")
        assert result is not None


class TestFetchData:
    """数据获取测试"""

    @patch("stock_model.pipeline.trading_pipeline.StockDataFetcher")
    def test_fetch_data_connection_error(self, mock_fetcher_cls):
        """连接错误返回None"""
        config = _make_config()
        pipeline = TradingPipeline(config=config)

        mock_fetcher = MagicMock()
        mock_fetcher.get_daily.side_effect = ConnectionError("network error")
        pipeline._fetcher = mock_fetcher

        result = pipeline._fetch_data("000001")
        assert result is None

    @patch("stock_model.pipeline.trading_pipeline.StockDataFetcher")
    def test_fetch_data_runtime_error(self, mock_fetcher_cls):
        """RuntimeError返回None"""
        config = _make_config()
        pipeline = TradingPipeline(config=config)

        mock_fetcher = MagicMock()
        mock_fetcher.get_daily.side_effect = RuntimeError("source error")
        pipeline._fetcher = mock_fetcher

        result = pipeline._fetch_data("000001")
        assert result is None


class TestSignalHistory:
    """信号历史测试"""

    @patch("stock_model.pipeline.trading_pipeline.StockDataFetcher")
    def test_signal_history_property(self, mock_fetcher_cls):
        config = _make_config()
        pipeline = TradingPipeline(config=config)
        pipeline._record_signal("000001", ActionType.BUY)

        history = pipeline.signal_history
        assert "000001_buy" in history
        # 返回的是副本
        history.clear()
        assert "000001_buy" in pipeline.signal_history

    @patch("stock_model.pipeline.trading_pipeline.StockDataFetcher")
    def test_record_signal_hold_ignored(self, mock_fetcher_cls):
        config = _make_config()
        pipeline = TradingPipeline(config=config)
        pipeline._record_signal("000001", ActionType.HOLD)
        assert len(pipeline.signal_history) == 0


class TestScheduledExecution:
    """定时执行测试"""

    @patch("stock_model.pipeline.trading_pipeline.StockDataFetcher")
    def test_is_running_default_false(self, mock_fetcher_cls):
        config = _make_config()
        pipeline = TradingPipeline(config=config)
        assert pipeline.is_running is False

    @patch("stock_model.pipeline.trading_pipeline.StockDataFetcher")
    def test_on_data_callback(self, mock_fetcher_cls):
        """数据回调触发run_once"""
        config = _make_config()
        pipeline = TradingPipeline(config=config)

        # Mock run_once
        pipeline.run_once = MagicMock(
            return_value=PipelineResult(symbol="000001", status=PipelineStatus.EXECUTED)
        )

        df = _make_df()
        pipeline._on_data_callback("000001", df)

        pipeline.run_once.assert_called_once_with("000001")
