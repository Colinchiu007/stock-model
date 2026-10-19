"""选股扫描提速三件套的测试

实测基线(2026-10-19, 本机):
  3~5 只         0.5s
  30 只 冷缓存   64.2s     <- 用户抱怨的"有点长"
  30 只 热缓存    5.0s

结论: 慢在**首次取数**(每只 ~2s), 不是计算; 串行是 baostock 单连接
不安全导致的既成事实(并发会 RuntimeError, 已复现), 不能靠加线程解决。

三个优化:
  1. 启动后台预热常用股票池 -> 首次扫描也接近热缓存速度
  2. 缓存 TTL 策略          -> 收盘后日线不再变, 不该 1 小时就失效
  3. 扫描进度反馈          -> 60 秒的转圈变成"已扫描 12/30"
"""

from __future__ import annotations

# ==================== 2. 缓存 TTL 策略 ====================


class TestCacheTTLPolicy:
    def test_ttl_long_after_market_close(self):
        """收盘后日线不再变化, TTL 应显著拉长"""
        from stock_model.data.fetcher import _effective_cache_ttl

        # 收盘后(15:30)
        assert _effective_cache_ttl(base_ttl=3600, now_hour=16) > 3600
        # 盘中保持短 TTL, 避免拿到隔夜前的旧数据
        assert _effective_cache_ttl(base_ttl=3600, now_hour=10) == 3600

    def test_ttl_policy_documented_constants(self):
        """策略常量有明确取值(避免魔法数字漂移)"""
        from stock_model.data import fetcher

        assert fetcher.CACHE_TTL_AFTER_CLOSE > fetcher.CACHE_TTL_INTRADAY


# ==================== 1. 启动预热 ====================


class TestWarmup:
    def test_warmup_does_not_raise_on_bad_symbol(self):
        """预热遇到坏标的必须跳过, 绝不能让服务启动失败"""
        from stock_model.data.fetcher import warmup_symbols

        class FakeFetcher:
            def get_daily(self, symbol, start_date=None, end_date=None):
                if symbol == "BAD":
                    raise RuntimeError("数据源挂了")
                import pandas as pd

                return pd.DataFrame({"close": [1.0, 2.0]}, index=[0, 1])

        ok, failed = warmup_symbols(FakeFetcher(), ["000001", "BAD", "600036"])
        assert ok == 2, f"应成功预热 2 只, 实际 {ok}"
        assert failed == 1, f"应记录 1 只失败, 实际 {failed}"

    def test_warmup_empty_input_is_noop(self):
        from stock_model.data.fetcher import warmup_symbols

        class FakeFetcher:
            def get_daily(self, symbol, start_date=None, end_date=None):  # pragma: no cover
                raise AssertionError("空列表不该发起任何取数")

        ok, failed = warmup_symbols(FakeFetcher(), [])
        assert (ok, failed) == (0, 0)


# ==================== 3. 扫描进度 ====================


class TestScanProgress:
    def test_progress_reports_incremental_counts(self, monkeypatch):
        """进度回调必须逐个累加, 而不是最后一次性给总数"""
        import pandas as pd

        df = pd.DataFrame(
            {
                "open": [1.0] * 70,
                "high": [1.0] * 70,
                "low": [1.0] * 70,
                "close": [1.0] * 70,
                "volume": [1] * 70,
            },
            index=pd.bdate_range("2024-01-01", periods=70),
        )

        class FakeFetcher:
            def __init__(self, source=None):
                self.source = source

            def get_daily(self, symbol, start_date=None, end_date=None):
                return df

        import stock_model.web.screener as scr

        # _analyze_one 内部做 `from stock_model.data.fetcher import StockDataFetcher`,
        # 故 patch 该模块属性即可拦住真实网络
        monkeypatch.setattr("stock_model.data.fetcher.StockDataFetcher", FakeFetcher, raising=True)
        from stock_model.strategy.manual import ManualStrategy

        seen: list[tuple[int, int]] = []

        def on_progress(done: int, total: int) -> None:
            seen.append((done, total))

        results = scr._scan_with_progress(
            ["000001", "000002", "600036"],
            "baostock",
            "20240101",
            ManualStrategy,
            60,
            on_progress=on_progress,
        )

        assert len(results) == 3
        assert seen, "必须上报进度"
        # 必须**逐只**上报(3 只 -> 恰好 3 次), 而不是只在结束时给一次总数。
        # 变异验证: 把 `on_progress(i, total)` 改成只在 i == total 时调用,
        # 若这里不锁死, 进度就退化成"永远 0/N"的假进度, 测试仍然会绿。
        assert seen == [(1, 3), (2, 3), (3, 3)], f"应逐只上报 1/3->2/3->3/3, 实际 {seen}"
        assert [d for d, _ in seen] == sorted(d for d, _ in seen), "进度必须单调递增"
        assert all(t == 3 for _, t in seen), "total 应固定为总只数"

    def test_progress_callback_error_does_not_break_scan(self, monkeypatch):
        """进度回调抛异常绝不能影响扫描结果(前端断线不该拖垮后端)"""
        import pandas as pd

        df = pd.DataFrame(
            {
                "open": [1.0] * 70,
                "high": [1.0] * 70,
                "low": [1.0] * 70,
                "close": [1.0] * 70,
                "volume": [1] * 70,
            },
            index=pd.bdate_range("2024-01-01", periods=70),
        )

        class FakeFetcher:
            def __init__(self, source=None):
                self.source = source

            def get_daily(self, symbol, start_date=None, end_date=None):
                return df

        import stock_model.web.screener as scr

        monkeypatch.setattr("stock_model.data.fetcher.StockDataFetcher", FakeFetcher, raising=True)
        from stock_model.strategy.manual import ManualStrategy

        def bad_progress(done: int, total: int) -> None:
            raise RuntimeError("前端断线了")

        results = scr._scan_with_progress(
            ["000001", "000002"],
            "baostock",
            "20240101",
            ManualStrategy,
            60,
            on_progress=bad_progress,
        )
        assert len(results) == 2, "进度回调异常不得让扫描少一只"
