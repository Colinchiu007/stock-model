"""AutoDataCollector 自动数据采集器测试"""

from unittest.mock import MagicMock, patch

import pandas as pd

from stock_model.data.collector import AutoDataCollector


def _make_df(n=10):
    """构造模拟行情数据"""
    return pd.DataFrame(
        {
            "open": range(n),
            "close": range(n),
            "high": range(n),
            "low": range(n),
            "volume": range(n),
        },
        index=pd.date_range("2024-01-01", periods=n, freq="B"),
    ).astype(float)


class TestCollectorInit:
    """初始化测试"""

    def test_default_init(self):
        collector = AutoDataCollector()
        assert collector.watchlist == []
        assert collector.is_running is False

    def test_stats_initial(self):
        collector = AutoDataCollector()
        stats = collector.stats
        assert stats["collect_count"] == 0
        assert stats["error_count"] == 0
        assert stats["watchlist_size"] == 0


class TestWatchlist:
    """监控列表管理测试"""

    def test_add_watchlist(self):
        collector = AutoDataCollector()
        collector.add_watchlist(["000001", "600000"])
        assert collector.watchlist == ["000001", "600000"]

    def test_add_watchlist_dedup(self):
        collector = AutoDataCollector()
        collector.add_watchlist(["000001", "000001"])
        assert collector.watchlist == ["000001"]

    def test_add_watchlist_strip(self):
        collector = AutoDataCollector()
        collector.add_watchlist([" 000001 "])
        assert collector.watchlist == ["000001"]

    def test_add_watchlist_skip_empty(self):
        collector = AutoDataCollector()
        collector.add_watchlist(["", "000001"])
        assert collector.watchlist == ["000001"]

    def test_remove_watchlist(self):
        collector = AutoDataCollector()
        collector.add_watchlist(["000001", "600000"])
        collector.remove_watchlist(["000001"])
        assert collector.watchlist == ["600000"]

    def test_remove_nonexistent(self):
        collector = AutoDataCollector()
        collector.add_watchlist(["000001"])
        collector.remove_watchlist(["999999"])  # 不存在，不报错
        assert collector.watchlist == ["000001"]


class TestCallbacks:
    """回调注册测试"""

    def test_on_data_callback(self):
        collector = AutoDataCollector()
        callback = MagicMock()
        collector.on_data(callback)
        assert callback in collector._callbacks

    def test_on_error_callback(self):
        collector = AutoDataCollector()
        callback = MagicMock()
        collector.on_error(callback)
        # error callback存储为tuple
        assert any(isinstance(cb, tuple) and cb[0] == "error" for cb in collector._callbacks)


class TestCollectNow:
    """collect_now 立即采集测试"""

    @patch.object(AutoDataCollector, "__init__", lambda self: None)
    def test_collect_now_success(self):
        collector = AutoDataCollector.__new__(AutoDataCollector)
        collector._watchlist = ["000001"]
        collector._callbacks = []
        collector._collect_count = 0
        collector._error_count = 0
        collector._last_collect_time = None

        mock_fetcher = MagicMock()
        mock_fetcher.get_daily.return_value = _make_df()
        collector._fetcher = mock_fetcher

        results = collector.collect_now()

        assert "000001" in results
        assert collector._collect_count == 1

    @patch.object(AutoDataCollector, "__init__", lambda self: None)
    def test_collect_now_empty_data(self):
        collector = AutoDataCollector.__new__(AutoDataCollector)
        collector._watchlist = ["000001"]
        collector._callbacks = []
        collector._collect_count = 0
        collector._error_count = 0
        collector._last_collect_time = None

        mock_fetcher = MagicMock()
        mock_fetcher.get_daily.return_value = pd.DataFrame()
        collector._fetcher = mock_fetcher

        results = collector.collect_now()

        assert "000001" not in results
        assert collector._error_count == 1

    @patch.object(AutoDataCollector, "__init__", lambda self: None)
    def test_collect_now_fetch_error(self):
        collector = AutoDataCollector.__new__(AutoDataCollector)
        collector._watchlist = ["000001"]
        collector._callbacks = []
        collector._collect_count = 0
        collector._error_count = 0
        collector._last_collect_time = None

        mock_fetcher = MagicMock()
        mock_fetcher.get_daily.side_effect = ConnectionError("network error")
        collector._fetcher = mock_fetcher

        results = collector.collect_now()

        assert "000001" not in results
        assert collector._error_count == 1

    @patch.object(AutoDataCollector, "__init__", lambda self: None)
    def test_collect_now_with_data_callback(self):
        results_received = []

        def on_data(symbol, df):
            results_received.append((symbol, len(df)))

        collector = AutoDataCollector.__new__(AutoDataCollector)
        collector._watchlist = ["000001"]
        collector._callbacks = [on_data]
        collector._collect_count = 0
        collector._error_count = 0
        collector._last_collect_time = None

        mock_fetcher = MagicMock()
        mock_fetcher.get_daily.return_value = _make_df()
        collector._fetcher = mock_fetcher

        collector.collect_now()

        assert len(results_received) == 1
        assert results_received[0][0] == "000001"

    @patch.object(AutoDataCollector, "__init__", lambda self: None)
    def test_collect_now_with_error_callback(self):
        errors_received = []

        def on_error(symbol, error):
            errors_received.append((symbol, str(error)))

        collector = AutoDataCollector.__new__(AutoDataCollector)
        collector._watchlist = ["000001"]
        collector._callbacks = [("error", on_error)]
        collector._collect_count = 0
        collector._error_count = 0
        collector._last_collect_time = None

        mock_fetcher = MagicMock()
        mock_fetcher.get_daily.side_effect = ConnectionError("network error")
        collector._fetcher = mock_fetcher

        collector.collect_now()

        assert len(errors_received) == 1


class TestScheduled:
    """定时采集测试"""

    def test_start_without_apscheduler(self):
        """未安装apscheduler时降级"""
        collector = AutoDataCollector()
        # 直接测试start方法，apscheduler可能已安装
        # 仅验证不崩溃
        try:
            collector.start(interval_minutes=60)
            collector.stop()
        except ImportError:
            pass  # 预期行为

    def test_stop_when_not_running(self):
        collector = AutoDataCollector()
        collector.stop()  # 不应报错
        assert collector.is_running is False
