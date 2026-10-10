"""
Batch 2 单元测试: 自动数据采集 + 数据质量监控 + 信号推送
"""

import json
import os
import tempfile
from unittest.mock import MagicMock

import numpy as np
import pandas as pd

from stock_model.data.collector import AutoDataCollector
from stock_model.data.monitor import DataQualityMonitor, QualityIssue, QualityReport
from stock_model.notify.channels import ConsoleChannel, FileChannel, WebhookChannel
from stock_model.notify.notifier import SignalNotifier
from stock_model.strategy.base import ActionType, StrategyResult

# --- 测试辅助 ---


def _make_test_df(days: int = 60) -> pd.DataFrame:
    """生成测试用行情数据"""
    np.random.seed(42)
    dates = pd.date_range("2024-01-01", periods=days, freq="D")
    close = 10.0 + np.cumsum(np.random.randn(days) * 0.1)
    close = np.maximum(close, 1.0)

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


# --- AutoDataCollector 测试 ---


class TestAutoDataCollector:
    def test_add_watchlist(self):
        collector = AutoDataCollector(fetcher=MagicMock())
        collector.add_watchlist(["000001", "600000"])
        assert "000001" in collector.watchlist
        assert "600000" in collector.watchlist

    def test_add_watchlist_no_duplicate(self):
        collector = AutoDataCollector(fetcher=MagicMock())
        collector.add_watchlist(["000001"])
        collector.add_watchlist(["000001"])
        assert collector.watchlist.count("000001") == 1

    def test_remove_watchlist(self):
        collector = AutoDataCollector(fetcher=MagicMock())
        collector.add_watchlist(["000001", "600000"])
        collector.remove_watchlist(["000001"])
        assert "000001" not in collector.watchlist
        assert "600000" in collector.watchlist

    def test_collect_now_success(self):
        mock_fetcher = MagicMock()
        mock_fetcher.get_daily.return_value = _make_test_df()

        collector = AutoDataCollector(fetcher=mock_fetcher)
        collector.add_watchlist(["000001"])

        results = collector.collect_now()

        assert "000001" in results
        assert len(results["000001"]) > 0
        assert collector.stats["collect_count"] == 1

    def test_collect_now_error(self):
        mock_fetcher = MagicMock()
        mock_fetcher.get_daily.side_effect = ConnectionError("网络错误")

        collector = AutoDataCollector(fetcher=mock_fetcher)
        collector.add_watchlist(["000001"])

        results = collector.collect_now()

        assert len(results) == 0
        assert collector.stats["error_count"] == 1

    def test_collect_now_callback(self):
        mock_fetcher = MagicMock()
        mock_fetcher.get_daily.return_value = _make_test_df()

        callback_data = []
        collector = AutoDataCollector(fetcher=mock_fetcher)
        collector.add_watchlist(["000001"])
        collector.on_data(lambda symbol, df: callback_data.append((symbol, len(df))))

        collector.collect_now()

        assert len(callback_data) == 1
        assert callback_data[0][0] == "000001"

    def test_stats(self):
        collector = AutoDataCollector(fetcher=MagicMock())
        stats = collector.stats
        assert "collect_count" in stats
        assert "error_count" in stats
        assert "watchlist_size" in stats

    def test_is_running_default(self):
        collector = AutoDataCollector(fetcher=MagicMock())
        assert not collector.is_running


# --- DataQualityMonitor 测试 ---


class TestDataQualityMonitor:
    def test_good_data(self):
        monitor = DataQualityMonitor()
        df = _make_test_df()
        report = monitor.check(df, symbol="000001")

        assert report.score > 80
        assert not report.has_errors

    def test_empty_data(self):
        monitor = DataQualityMonitor()
        report = monitor.check(pd.DataFrame(), symbol="000001")

        assert report.score == 0.0
        assert report.has_errors

    def test_none_data(self):
        monitor = DataQualityMonitor()
        report = monitor.check(None, symbol="000001")

        assert report.score == 0.0
        assert report.has_errors

    def test_missing_values(self):
        monitor = DataQualityMonitor(max_missing_pct=0.01)
        df = _make_test_df()
        # 人为制造缺失
        df.iloc[0:5, 0] = np.nan
        df.iloc[10:15, 1] = np.nan

        report = monitor.check(df, symbol="000001")

        missing_issues = [i for i in report.issues if i.category == "missing"]
        assert len(missing_issues) > 0

    def test_missing_close_column(self):
        monitor = DataQualityMonitor()
        df = pd.DataFrame({"open": [1.0], "high": [2.0]})
        report = monitor.check(df, symbol="000001")

        schema_issues = [i for i in report.issues if i.category == "schema"]
        assert len(schema_issues) > 0
        assert report.has_errors

    def test_stale_data(self):
        monitor = DataQualityMonitor(staleness_hours=24)
        # 创建过期数据(1年前)
        dates = pd.date_range("2023-01-01", periods=60, freq="D")
        df = pd.DataFrame(
            {"close": np.random.randn(60) + 10},
            index=dates,
        )

        report = monitor.check(df, symbol="000001")

        staleness_issues = [i for i in report.issues if i.category == "staleness"]
        assert len(staleness_issues) > 0

    def test_score_calculation(self):
        monitor = DataQualityMonitor()
        df = _make_test_df()
        report = monitor.check(df, symbol="000001")

        assert 0 <= report.score <= 100


# --- QualityIssue/QualityReport 测试 ---


class TestQualityIssue:
    def test_str(self):
        issue = QualityIssue(severity="error", category="missing", message="缺失数据")
        assert "ERROR" in str(issue)
        assert "missing" in str(issue)


class TestQualityReport:
    def test_has_errors(self):
        report = QualityReport(
            symbol="000001",
            issues=[QualityIssue(severity="error", category="schema", message="test")],
        )
        assert report.has_errors

    def test_no_errors(self):
        report = QualityReport(
            symbol="000001",
            issues=[QualityIssue(severity="warning", category="missing", message="test")],
        )
        assert not report.has_errors

    def test_str(self):
        report = QualityReport(symbol="000001", score=85.0)
        assert "000001" in str(report)
        assert "85" in str(report)


# --- SignalNotifier 测试 ---


class TestSignalNotifier:
    def test_notify_with_console(self, capsys):
        notifier = SignalNotifier()
        notifier.add_channel(ConsoleChannel())

        result = StrategyResult(
            symbol="000001", action=ActionType.BUY, confidence=0.8, reason="测试信号"
        )
        notifier.notify(result)

        captured = capsys.readouterr()
        assert "000001" in captured.out
        assert "BUY" in captured.out

    def test_notify_with_file(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            filepath = f.name

        try:
            notifier = SignalNotifier()
            notifier.add_channel(FileChannel(filepath))

            result = StrategyResult(
                symbol="000001", action=ActionType.BUY, confidence=0.8, reason="测试信号"
            )
            notifier.notify(result)

            with open(filepath, encoding="utf-8") as f:
                lines = f.readlines()
                assert len(lines) == 1
                record = json.loads(lines[0])
                assert record["symbol"] == "000001"
                assert record["action"] == "buy"
        finally:
            os.unlink(filepath)

    def test_notify_no_channels(self):
        notifier = SignalNotifier()
        result = StrategyResult(
            symbol="000001", action=ActionType.HOLD, confidence=0.5, reason="无通道"
        )
        # 不应抛异常
        notifier.notify(result)

    def test_add_remove_channel(self):
        notifier = SignalNotifier()
        channel = ConsoleChannel()
        notifier.add_channel(channel)
        assert len(notifier._channels) == 1

        notifier.remove_channel(channel)
        assert len(notifier._channels) == 0

    def test_format_message_buy(self):
        notifier = SignalNotifier()
        result = StrategyResult(
            symbol="000001",
            action=ActionType.BUY,
            confidence=0.8,
            reason="均线金叉",
            target_price=12.0,
            stop_loss=10.0,
            position_pct=60,
        )
        message = notifier._format_message(result)

        assert "000001" in message
        assert "BUY" in message
        assert "12.00" in message
        assert "10.00" in message
        assert "60%" in message


# --- WebhookChannel 测试 ---


class TestWebhookChannel:
    def test_webhook_without_httpx(self):
        """httpx未安装时不抛异常"""
        channel = WebhookChannel(url="http://example.com/webhook")
        result = StrategyResult(
            symbol="000001", action=ActionType.BUY, confidence=0.8, reason="测试"
        )
        # 不应抛异常(即使httpx未安装或URL不可达)
        channel.send("test message", result)
