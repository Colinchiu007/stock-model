"""选股扫描提速的测试

实测基线(2026-10-19, 本机):
  3~5 只         0.5s
  30 只 冷缓存   64.2s     <- 用户抱怨的"有点长"
  30 只 热缓存    5.0s

三个优化:
  1. 启动后台预热常用股票池 -> 首次扫描也接近热缓存速度
  2. 缓存 TTL 策略          -> 收盘后日线不再变, 不该 1 小时就失效
  3. 扫描进度反馈          -> 60 秒的转圈变成"已扫描 12/30"

⚠️ 归因修正(同日 16:40 实测): 上面第 1、2 条只解决了"取数慢", 但**那不是主因**。
真正的瓶颈是"每只股票新建 fetcher + 每只新建策略", 而这两处都会间接触发
数据源构造(``BaostockSource.__init__`` 里就 login), 数据源不可用时每次要等
21~94 秒超时:

  修复前(接口实测): 扫 5 只 = 354s / 196s
  修复后(接口实测): 扫 5 只 = **0.1s**

缓存命中后取数只要 0.09s、指标 0.03s、信号 0.00s —— 99% 的时间花在等
login 超时上。TestFetcherReuse / TestLazyFetcher 锁住这两处。
"""

from __future__ import annotations

# ==================== 归因修正: 真正的瓶颈(耗时占 99%) ====================


class TestFetcherReuse:
    """同一次扫描内必须复用同一个 fetcher"""

    def test_same_source_returns_same_instance(self, monkeypatch):
        import stock_model.web.screener as scr

        made: list[str] = []

        class FakeFetcher:
            def __init__(self, source=None):
                made.append(source or "")

        scr._fetcher_cache.clear()
        monkeypatch.setattr(scr, "StockDataFetcher", FakeFetcher)
        a = scr._fetcher_for("sina")
        b = scr._fetcher_for("sina")
        scr._fetcher_cache.clear()

        assert a is b, "同一 data_source 必须复用同一个 fetcher 实例"
        assert made == ["sina"], f"不应重复构造(实际构造 {len(made)} 次)"

    def test_different_sources_get_different_instances(self, monkeypatch):
        """不同 data_source 各一个, 不能串味"""
        import stock_model.web.screener as scr

        class FakeFetcher:
            def __init__(self, source=None):
                self.source = source

        scr._fetcher_cache.clear()
        monkeypatch.setattr(scr, "StockDataFetcher", FakeFetcher)
        a = scr._fetcher_for("sina")
        b = scr._fetcher_for("akshare")
        scr._fetcher_cache.clear()

        assert a is not b, "不同数据源不能共用一个 fetcher"


class TestLazyFetcher:
    """FundamentalAnalysis 的 fetcher 必须惰性创建"""

    def test_no_fetcher_constructed_on_init(self, monkeypatch):
        """构造不该触发 StockDataFetcher()(它会 login 等超时)"""
        import stock_model.analysis.fundamental as fa

        made: list[str] = []

        class Boom:
            def __init__(self, *a, **k):
                made.append("x")
                raise AssertionError("构造时不应创建 fetcher")

        monkeypatch.setattr(fa, "StockDataFetcher", Boom)
        fa.FundamentalAnalysis()
        assert made == [], "构造 FundamentalAnalysis 不应创建 fetcher"

    def test_injected_fetcher_used_immediately(self):
        """显式注入的 fetcher 立即可用"""
        from stock_model.analysis.fundamental import FundamentalAnalysis

        sentinel = object()
        obj = FundamentalAnalysis(fetcher=sentinel)  # type: ignore[arg-type]
        assert obj.fetcher is sentinel

    def test_fetcher_created_on_first_use(self, monkeypatch):
        """真正用到时才创建, 且只创建一次"""
        import stock_model.analysis.fundamental as fa

        made: list[str] = []

        class Fake:
            def __init__(self, *a, **k):
                made.append("x")

        monkeypatch.setattr(fa, "StockDataFetcher", Fake)
        obj = fa.FundamentalAnalysis()
        assert made == []
        _ = obj.fetcher
        _ = obj.fetcher
        assert len(made) == 1, f"应只创建一次, 实际 {len(made)} 次"

    def test_fetcher_settable(self):
        """setter 可用(兼容既有代码直接赋值)"""
        from stock_model.analysis.fundamental import FundamentalAnalysis

        obj = FundamentalAnalysis()
        sentinel = object()
        obj.fetcher = sentinel  # type: ignore[assignment]
        assert obj.fetcher is sentinel


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
