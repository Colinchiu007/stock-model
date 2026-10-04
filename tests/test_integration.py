"""
集成测试

验证模块间协作的端到端流程:
  - 数据获取 → 技术分析 → 信号生成
  - 策略执行 → 风控检查 → 仓位计算 → 信号推送
  - TradingPipeline 完整流水线
  - 回测引擎端到端
"""

from __future__ import annotations

from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from stock_model.analysis.signals import SignalGenerator
from stock_model.analysis.technical import TechnicalAnalysis
from stock_model.data.fetcher import StockDataFetcher
from stock_model.data.monitor import DataQualityMonitor
from stock_model.notify.channels import ConsoleChannel
from stock_model.notify.notifier import SignalNotifier
from stock_model.pipeline.config import PipelineConfig
from stock_model.pipeline.models import PipelineResult, PipelineRunSummary, PipelineStatus
from stock_model.pipeline.trading_pipeline import TradingPipeline
from stock_model.risk.manager import RiskManager
from stock_model.risk.models import Position
from stock_model.risk.position_sizer import PositionSizer
from stock_model.strategy.base import ActionType, StrategyResult
from stock_model.strategy.engine import BacktestEngine
from stock_model.strategy.manual import ManualStrategy

# ==================== Fixtures ====================


@pytest.fixture
def sample_df():
    """生成测试用行情数据"""
    np.random.seed(42)
    n = 120
    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    close = 10.0 + np.cumsum(np.random.randn(n) * 0.2)
    close = np.maximum(close, 1.0)  # 确保价格为正

    df = pd.DataFrame(
        {
            "open": close + np.random.randn(n) * 0.1,
            "high": close + abs(np.random.randn(n) * 0.2),
            "low": close - abs(np.random.randn(n) * 0.2),
            "close": close,
            "volume": np.random.randint(10000, 100000, n).astype(float),
        },
        index=dates,
    )
    # 确保 open/high/low 合理
    df["open"] = df["close"] + np.random.randn(n) * 0.05
    df["high"] = df[["open", "close"]].max(axis=1) + abs(np.random.randn(n) * 0.1)
    df["low"] = df[["open", "close"]].min(axis=1) - abs(np.random.randn(n) * 0.1)
    df["volume"] = df["volume"].astype(float)
    return df


@pytest.fixture
def pipeline_config():
    """测试用Pipeline配置"""
    return PipelineConfig(
        watchlist=["000001"],
        start_date="20240101",
        min_data_rows=30,
        min_quality_score=0.3,
        signal_cooldown_minutes=0,  # 测试时关闭冷却期
        initial_capital=100000.0,
        position_method="fixed",
        position_fixed_pct=0.10,
        enable_notify=False,  # 测试时关闭通知
    )


# ==================== 数据→分析→信号 链路 ====================


class TestDataAnalysisSignalChain:
    """数据获取 → 技术分析 → 信号生成 集成测试"""

    def test_full_chain(self, sample_df):
        """完整链路: 技术分析 → 信号生成"""
        # 1. 技术分析
        ta = TechnicalAnalysis()
        df_with_indicators = ta.analyze_all(sample_df)

        # 验证指标列已添加
        assert "ma5" in df_with_indicators.columns
        assert "ma10" in df_with_indicators.columns
        assert "ema5" in df_with_indicators.columns
        assert "kdj_k" in df_with_indicators.columns

        # 2. 信号生成
        signal_gen = SignalGenerator()
        signals = signal_gen.generate_all(df_with_indicators, "000001")

        # 验证信号列表
        assert isinstance(signals, list)
        # 信号可能为空(取决于数据), 但不应报错

    def test_data_quality_then_analysis(self, sample_df):
        """数据质量检查 → 技术分析"""
        # 1. 质量检查
        monitor = DataQualityMonitor()
        report = monitor.check(sample_df, symbol="000001")

        assert report.score > 0
        assert isinstance(report.issues, list)

        # 2. 质量通过后执行分析
        if report.score >= 0.5:
            ta = TechnicalAnalysis()
            df = ta.analyze_all(sample_df)
            assert len(df.columns) > len(sample_df.columns)


# ==================== 策略→风控→仓位→通知 链路 ====================


class TestStrategyRiskPositionChain:
    """策略执行 → 风控检查 → 仓位计算 → 信号通知 集成测试"""

    def test_buy_signal_flow(self, sample_df):
        """买入信号完整流程"""
        # 1. 策略执行
        strategy = ManualStrategy()
        result = strategy.analyze("000001", sample_df)

        assert isinstance(result, StrategyResult)
        assert result.action in [ActionType.BUY, ActionType.SELL, ActionType.HOLD]

        # 2. 风控检查
        rm = RiskManager()
        position = Position(
            symbol="000001",
            shares=1000,
            cost_price=10.0,
            current_price=sample_df["close"].iloc[-1],
        )
        alerts = rm.check_position_risk(position)
        assert isinstance(alerts, list)

        # 3. 仓位计算
        sizer = PositionSizer()
        shares = sizer.fixed_size(capital=100000, price=sample_df["close"].iloc[-1])
        assert shares >= 100  # 最小100股

        # 4. 通知(不报错即可)
        notifier = SignalNotifier()
        notifier.add_channel(ConsoleChannel())
        notifier.notify(result)  # 不应抛异常

    def test_risk_blocks_signal(self):
        """风控拦截严重风险信号"""
        rm = RiskManager(default_stop_loss_pct=0.05)

        # 模拟深度亏损持仓(触发止损)
        position = Position(
            symbol="000001",
            shares=1000,
            cost_price=10.0,
            current_price=9.0,  # 亏损10%
        )
        alerts = rm.check_position_risk(position)

        # 应有止损警报
        stop_loss_alerts = [a for a in alerts if a.type.value == "stop_loss"]
        assert len(stop_loss_alerts) > 0

    def test_position_sizer_methods(self):
        """仓位计算多种方法"""
        sizer = PositionSizer(max_position_pct=0.20, min_shares=100)

        # 固定仓位
        shares = sizer.fixed_size(capital=100000, price=10.0, position_pct=0.10)
        assert shares >= 100

        # 凯利公式(正期望)
        shares = sizer.kelly_size(
            capital=100000, price=10.0, win_rate=0.6, avg_win=0.05, avg_loss=0.03
        )
        assert shares >= 0  # 可能返回0

        # ATR仓位
        shares = sizer.atr_size(capital=100000, price=10.0, atr=0.5)
        assert shares >= 0


# ==================== TradingPipeline 集成测试 ====================


class TestTradingPipeline:
    """TradingPipeline 完整流水线测试"""

    @patch.object(StockDataFetcher, "get_daily")
    def test_run_once_success(self, mock_get_daily, pipeline_config, sample_df):
        """单次执行成功"""
        mock_get_daily.return_value = sample_df

        pipeline = TradingPipeline(config=pipeline_config)
        pipeline.add_strategy(ManualStrategy())

        result = pipeline.run_once("000001")

        assert isinstance(result, PipelineResult)
        assert result.symbol == "000001"
        assert result.status in [PipelineStatus.EXECUTED, PipelineStatus.SKIPPED]
        assert result.quality_score >= 0

    @patch.object(StockDataFetcher, "get_daily")
    def test_run_once_no_strategy(self, mock_get_daily, pipeline_config, sample_df):
        """未注册策略时跳过"""
        mock_get_daily.return_value = sample_df

        pipeline = TradingPipeline(config=pipeline_config)
        # 不注册策略

        result = pipeline.run_once("000001")

        assert result.status == PipelineStatus.SKIPPED
        assert "未注册" in result.reason

    @patch.object(StockDataFetcher, "get_daily")
    def test_run_once_data_fetch_failure(self, mock_get_daily, pipeline_config):
        """数据获取失败"""
        mock_get_daily.return_value = None

        pipeline = TradingPipeline(config=pipeline_config)
        pipeline.add_strategy(ManualStrategy())

        result = pipeline.run_once("000001")

        assert result.status == PipelineStatus.SKIPPED
        assert "数据不足" in result.reason

    @patch.object(StockDataFetcher, "get_daily")
    def test_run_once_insufficient_data(self, mock_get_daily, pipeline_config):
        """数据行数不足"""
        # 只返回10行数据(低于min_data_rows=30)
        small_df = pd.DataFrame(
            {
                "open": range(10),
                "high": range(10),
                "low": range(10),
                "close": range(10),
                "volume": range(10),
            },
            index=pd.date_range("2024-01-01", periods=10, freq="B"),
        ).astype(float)
        mock_get_daily.return_value = small_df

        pipeline = TradingPipeline(config=pipeline_config)
        pipeline.add_strategy(ManualStrategy())

        result = pipeline.run_once("000001")

        assert result.status == PipelineStatus.SKIPPED
        assert "数据不足" in result.reason

    @patch.object(StockDataFetcher, "get_daily")
    def test_run_batch(self, mock_get_daily, sample_df):
        """批量执行"""
        mock_get_daily.return_value = sample_df

        config = PipelineConfig(
            watchlist=["000001", "600036"],
            min_data_rows=30,
            min_quality_score=0.3,
            signal_cooldown_minutes=0,
            enable_notify=False,
        )
        pipeline = TradingPipeline(config=config)
        pipeline.add_strategy(ManualStrategy())

        summary = pipeline.run_batch()

        assert isinstance(summary, PipelineRunSummary)
        assert summary.total == 2
        assert summary.run_time >= 0

    @patch.object(StockDataFetcher, "get_daily")
    def test_signal_cooldown(self, mock_get_daily, sample_df):
        """信号冷却期测试"""
        mock_get_daily.return_value = sample_df

        config = PipelineConfig(
            watchlist=["000001"],
            min_data_rows=30,
            min_quality_score=0.3,
            signal_cooldown_minutes=60,  # 60分钟冷却
            enable_notify=False,
        )
        pipeline = TradingPipeline(config=config)
        pipeline.add_strategy(ManualStrategy())

        # 第一次执行
        result1 = pipeline.run_once("000001")

        # 第二次执行(应被冷却)
        result2 = pipeline.run_once("000001")
        if result1.status == PipelineStatus.EXECUTED and result1.strategy_result:
            if result1.strategy_result.action != ActionType.HOLD:
                assert result2.status == PipelineStatus.SKIPPED
                assert "冷却" in result2.reason

    def test_cooldown_clear(self, pipeline_config):
        """冷却期清除"""
        pipeline = TradingPipeline(config=pipeline_config)
        pipeline._record_signal("000001", ActionType.BUY)

        assert len(pipeline.signal_history) == 1
        pipeline.clear_signal_history()
        assert len(pipeline.signal_history) == 0

    @patch.object(StockDataFetcher, "get_daily")
    def test_on_result_callback(self, mock_get_daily, pipeline_config, sample_df):
        """结果回调测试"""
        mock_get_daily.return_value = sample_df

        pipeline = TradingPipeline(config=pipeline_config)
        pipeline.add_strategy(ManualStrategy())

        callback_results = []
        pipeline.on_result(lambda r: callback_results.append(r))

        pipeline.run_once("000001")

        assert len(callback_results) == 1
        assert isinstance(callback_results[0], PipelineResult)

    @patch.object(StockDataFetcher, "get_daily")
    def test_pipeline_stats(self, mock_get_daily, pipeline_config, sample_df):
        """流水线统计信息"""
        mock_get_daily.return_value = sample_df

        pipeline = TradingPipeline(config=pipeline_config)
        pipeline.add_strategy(ManualStrategy())

        stats = pipeline.stats
        assert "strategies" in stats
        assert "watchlist" in stats
        assert "is_running" in stats


# ==================== 回测引擎集成测试 ====================


class TestBacktestIntegration:
    """回测引擎端到端测试"""

    def test_backtest_manual_strategy(self, sample_df):
        """ManualStrategy回测"""
        strategy = ManualStrategy()
        engine = BacktestEngine(initial_cash=100000)

        result = engine.run(strategy, sample_df, symbol="000001")

        assert result.strategy_name == "manual"
        assert result.symbol == "000001"
        assert result.initial_cash == 100000
        assert isinstance(result.final_cash, float)
        assert isinstance(result.trades, list)
        assert isinstance(result.metrics, dict)
        assert "total_return" in result.metrics

    def test_backtest_metrics(self, sample_df):
        """回测指标完整性"""
        strategy = ManualStrategy()
        engine = BacktestEngine(initial_cash=100000)

        result = engine.run(strategy, sample_df, symbol="000001")
        metrics = result.metrics

        # 验证关键指标存在
        expected_keys = ["total_return", "annual_return", "max_drawdown", "total_trades"]
        for key in expected_keys:
            assert key in metrics, f"缺少指标: {key}"


# ==================== PipelineConfig 测试 ====================


class TestPipelineConfig:
    """PipelineConfig 配置测试"""

    def test_default_config(self):
        """默认配置"""
        config = PipelineConfig()
        assert config.watchlist == ["000001", "600036"]
        assert config.data_source == "akshare"
        assert config.initial_capital == 100000.0
        assert config.signal_cooldown_minutes == 60
        assert config.enable_notify is True

    def test_custom_config(self):
        """自定义配置"""
        config = PipelineConfig(
            watchlist=["000001"],
            initial_capital=50000.0,
            signal_cooldown_minutes=30,
            position_method="kelly",
        )
        assert config.watchlist == ["000001"]
        assert config.initial_capital == 50000.0
        assert config.position_method == "kelly"

    def test_from_dict(self):
        """从字典创建配置"""
        data = {"watchlist": ["600036"], "initial_capital": 200000.0, "unknown_key": "ignore"}
        config = PipelineConfig.from_dict(data)
        assert config.watchlist == ["600036"]
        assert config.initial_capital == 200000.0
        # 未知字段被忽略

    def test_to_dict(self):
        """导出为字典"""
        config = PipelineConfig(watchlist=["000001"])
        d = config.to_dict()
        assert isinstance(d, dict)
        assert d["watchlist"] == ["000001"]
        assert "initial_capital" in d


# ==================== PipelineResult 测试 ====================


class TestPipelineResult:
    """PipelineResult 数据模型测试"""

    def test_executed_result(self):
        """执行成功结果"""
        result = PipelineResult(
            symbol="000001",
            status=PipelineStatus.EXECUTED,
            quality_score=0.85,
        )
        assert "executed" in str(result)
        assert "000001" in str(result)

    def test_skipped_result(self):
        """跳过结果"""
        result = PipelineResult(
            symbol="000001",
            status=PipelineStatus.SKIPPED,
            reason="数据不足",
        )
        assert "skipped" in str(result)
        assert "数据不足" in str(result)

    def test_blocked_result(self):
        """拦截结果"""
        result = PipelineResult(
            symbol="000001",
            status=PipelineStatus.BLOCKED,
            reason="风控拦截",
        )
        assert "blocked" in str(result)

    def test_error_result(self):
        """异常结果"""
        result = PipelineResult(
            symbol="000001",
            status=PipelineStatus.ERROR,
            error="ConnectionError",
        )
        assert "error" in str(result)


class TestPipelineRunSummary:
    """PipelineRunSummary 测试"""

    def test_summary_stats(self):
        """摘要统计"""
        summary = PipelineRunSummary(
            total=5,
            executed=3,
            skipped=1,
            blocked=1,
            errors=0,
            run_time=2.5,
        )
        assert summary.success_rate == 0.6
        assert "总计=5" in str(summary)
        assert "成功率=60%" in str(summary)

    def test_empty_summary(self):
        """空摘要"""
        summary = PipelineRunSummary()
        assert summary.success_rate == 0.0
