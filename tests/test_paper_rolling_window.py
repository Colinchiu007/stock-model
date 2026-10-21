"""模拟盘"滚动窗口"起点 —— 别再跑 2 年前的历史

缺陷(2026-10-21 用户实测发现):
  账户落盘 start_date=20240101 写死, 于是每交易日推进一根 bar, 从 2024-01-02
  开始爬。今天是 2026-10-21, cursor 才走到 2024-04-17 —— 差 **2.4 年**。
  按这速度永远在跑历史: 攒 20 笔成交要一两年, 且评估的是 2024 年的市场,
  对当下没有参考价值(用户明确要求"2-4 周内 ≥20 笔").

根因: ``_default_start_date`` 返回**绝对日期**而非"最近 N 根"的相对窗口。
`_build_engine` 恢复时又只用落盘的 start_date, 从不重新对齐。

契约(方案 A):
  1. 起点由**滚动窗口长度**决定(默认 300 根), 而非写死的绝对日期
  2. 300 根覆盖最长指标 MA250(实测 250 根零 NaN, 300 留余量)
  3. 窗口起点必须是真实交易日(不能是数据里不存在的日期)
  4. 落盘 start_date 缺省/异常时回退到窗口起点, 不抛异常
"""

from __future__ import annotations

import pytest

WINDOW = 300


class TestRollingWindow:
    def test_window_default_is_300(self):
        """默认 300 根(覆盖 MA250 + 余量)"""
        from stock_model.web.paper_api import DEFAULT_LOOKBACK_BARS

        assert DEFAULT_LOOKBACK_BARS == WINDOW

    def test_window_start_is_a_real_trading_day(self):
        """窗口起点必须是数据里真实存在的交易日"""
        from stock_model.web.paper_api import _window_start_date

        idx = _fake_index()
        start = _window_start_date(idx, WINDOW)
        assert start in {str(i.date()).replace("-", "") for i in idx}, f"{start} 不是真实交易日"

    def test_window_start_covers_indicator_lookback(self):
        """窗口内 bar 数 >= 250(MA250 不 NaN)"""
        from stock_model.web.paper_api import _window_start_date

        idx = _fake_index()
        start = _window_start_date(idx, WINDOW)
        kept = [i for i in idx if str(i.date()).replace("-", "") >= start]
        assert len(kept) >= 250, f"窗口只覆盖 {len(kept)} 根, 不足 MA250"

    def test_window_keeps_last_n_bars(self):
        """窗口保留**最后** N 根(而不是前 N 根) —— 否则还是在跑历史"""
        from stock_model.web.paper_api import _window_start_date

        idx = _fake_index(n=800)
        start = _window_start_date(idx, WINDOW)
        kept = [i for i in idx if str(i.date()).replace("-", "") >= start]
        assert len(kept) == WINDOW, f"应恰好保留 {WINDOW} 根, 实际 {len(kept)}"
        assert kept[-1] == idx[-1], "窗口末根必须等于数据末根(追到当下)"

    def test_window_short_data_uses_earliest(self):
        """数据不足一个窗口时用最早一根(不能更短, 否则指标全 NaN)"""
        from stock_model.web.paper_api import _window_start_date

        idx = _fake_index(n=100)
        assert _window_start_date(idx, WINDOW) == str(idx[0].date()).replace("-", "")

    def test_window_start_format(self):
        """返回 YYYYMMDD(与落盘/取数参数同格式)"""
        from stock_model.web.paper_api import _window_start_date

        s = _window_start_date(_fake_index(), WINDOW)
        assert len(s) == 8 and s.isdigit(), f"应为 YYYYMMDD, 实际 {s!r}"

    def test_empty_index_returns_default(self):
        """空索引回退默认起点(不能崩)"""
        from stock_model.web.paper_api import _window_start_date

        out = _window_start_date([], WINDOW)
        assert len(out) == 8 and out.isdigit()


class TestEngineStartDate:
    def test_build_uses_window_start_not_hardcoded(self, monkeypatch):
        """恢复时 start_date 必须由窗口推出, 不是写死的 20240101"""
        import stock_model.web.paper_api as pa

        seen: dict[str, str] = {}

        class FakeEngine:
            def __init__(self, **kw):
                seen.update(kw)
                self.symbols = kw.get("symbols") or []
                self._cursor = {}

            def add_strategy(self, s):
                return None

            def load_state(self, st):
                return None

        monkeypatch.setattr(pa, "_default_start_date", lambda syms: "20190101")
        monkeypatch.setattr("stock_model.paper.engine.PaperEngine", FakeEngine)
        monkeypatch.setattr(pa, "StockDataFetcher", _FakeFetcher(), raising=False)

        pa._build_engine("default", {"symbols": ["000002"]}, None)
        assert seen.get("start_date") not in ("20240101", "20190101"), (
            f"start_date 仍是写死的绝对日期: {seen.get('start_date')}"
        )
        assert len(seen.get("start_date", "")) == 8

    def test_explicit_metadata_start_date_respected(self, monkeypatch):
        """落盘显式配了 start_date 就按它来(可回退到旧行为)"""
        import stock_model.web.paper_api as pa

        seen: dict[str, str] = {}

        class FakeEngine:
            def __init__(self, **kw):
                seen.update(kw)
                self.symbols = kw.get("symbols") or []
                self._cursor = {}

            def add_strategy(self, s):
                return None

            def load_state(self, st):
                return None

        monkeypatch.setattr("stock_model.paper.engine.PaperEngine", FakeEngine)
        monkeypatch.setattr(pa, "StockDataFetcher", _FakeFetcher(), raising=False)

        pa._build_engine("default", {"symbols": ["000002"], "start_date": "20200101"}, None)
        assert seen.get("start_date") == "20200101", "显式配置应被尊重"


class TestRollingStartResilience:
    """起点算不出时必须回退, 不能把优化变成硬依赖"""

    def test_fetch_failure_falls_back_to_default(self, monkeypatch):
        """取数失败 -> 回退默认起点(账户仍能打开)"""
        import stock_model.web.paper_api as pa

        class Boom:
            def __init__(self, source=None):
                raise ConnectionError("数据源挂了")

        # _rolling_start_date 内部是 `from stock_model.data.fetcher import ...`,
        # 故 patch 源模块属性(与 test_sina_source 同款教训)
        monkeypatch.setattr("stock_model.data.fetcher.StockDataFetcher", Boom)
        out = pa._rolling_start_date(["000002"], "baostock")
        assert out == pa._default_start_date(["000002"]), f"取数失败应回退默认起点, 实际 {out}"

    def test_empty_data_falls_back_to_default(self, monkeypatch):
        """取回空数据 -> 回退默认起点"""
        import stock_model.web.paper_api as pa

        class Empty:
            def __init__(self, source=None):
                pass

            def get_daily(self, symbol, start_date=None, end_date=None):
                import pandas as pd

                return pd.DataFrame()

        monkeypatch.setattr("stock_model.data.fetcher.StockDataFetcher", Empty)
        assert pa._rolling_start_date(["000002"], "sina") == pa._default_start_date(["000002"])

    def test_window_tracks_latest_data(self):
        """窗口末根 = 数据末根(真实数据, 证明追到当下而非跑历史)"""

        import stock_model.web.paper_api as pa
        from stock_model.data.fetcher import StockDataFetcher as RealFetcher

        f = RealFetcher(source="baostock")
        bars = f.get_daily("000002", start_date=None)
        if bars is None or bars.empty:
            self.skipTest("baostock 不可用")
        start = pa._window_start_date(bars.index)
        kept = [i for i in bars.index if str(i.date()).replace("-", "") >= start]
        assert kept[-1] == bars.index[-1], "窗口末根必须等于数据末根"
        assert len(kept) == pa.DEFAULT_LOOKBACK_BARS


def _fake_index(n: int = 800):
    import pandas as pd

    return pd.bdate_range("2023-06-01", periods=n)


class _FakeFetcher:
    def get_daily(self, symbol, start_date=None, end_date=None):
        import pandas as pd

        idx = pd.bdate_range("2023-06-01", periods=800)
        return pd.DataFrame(
            {
                "open": [1.0] * 800,
                "high": [1.0] * 800,
                "low": [1.0] * 800,
                "close": [1.0] * 800,
                "volume": [1] * 800,
            },
            index=idx,
        )


@pytest.fixture(autouse=True)
def _patch_fetcher(monkeypatch):
    """_window_start_date 需要真实索引: 用假 fetcher 提供"""
    import stock_model.web.paper_api as pa

    monkeypatch.setattr(pa, "StockDataFetcher", _FakeFetcher, raising=False)
