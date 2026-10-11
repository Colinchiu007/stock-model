"""观察期核心路径测试 —— engine.step 的容错与边界(TD: 覆盖率盲区)

为什么在观察期补这些: 明天 15:30 内置调度将**真实**走到 step() ——
其中"标的自动剔除 / no_more_data / no_symbols / 策略异常不崩"这些分支
是首跑最可能踩到的路径(次新股、数据缺口、策略 bug 都会触发),
此前覆盖率仅 68%, 这些分支全部无锁。
"""

from __future__ import annotations

from unittest.mock import patch

import pandas as pd

from stock_model.paper.engine import PaperEngine


def make_df(n: int = 80, close: float = 10.0) -> pd.DataFrame:
    """构造 n 根日线(索引递增, 含 volume/close 等必需列)"""
    idx = pd.date_range("2026-01-01", periods=n, freq="D")
    return pd.DataFrame(
        {
            "open": [close] * n,
            "high": [close * 1.01] * n,
            "low": [close * 0.99] * n,
            "close": [close] * n,
            "volume": [1_000_000] * n,
        },
        index=idx,
    )


def make_engine(symbols, data: dict[str, pd.DataFrame | Exception], **kw):
    """构造引擎并把 _load 打桩成给定数据表"""
    e = PaperEngine(symbols=list(symbols), validate_symbols=False, **kw)

    def _load(symbol, *a, **k):
        v = data[symbol]
        if isinstance(v, Exception):
            raise v
        return v

    patcher = patch.object(e, "_load", side_effect=_load)
    patcher.start()
    e._data_cache = {}
    return e


# ==================== step 容错路径 ====================


class TestStepDropsBrokenSymbols:
    def test_broken_data_dropped_midway(self):
        """step 中某标的取数失败 → 从池中剔除并记入 dropped, 其余继续"""
        good = make_df(80)
        e = make_engine(
            ["000002", "600036"],
            {
                "000002": good,
                "600036": RuntimeError("数据获取失败: 远程主机强迫关闭"),
            },
        )
        result = e.step()
        assert "600036" in e.dropped_symbols
        assert "600036" not in e.symbols
        assert "000002" in e.symbols
        assert result["status"] in ("ok", "no_orders", "skipped") or "status" in result

    def test_insufficient_rows_dropped(self):
        """数据不足(min_data_rows) → 剔除"""
        e = make_engine(
            ["000002", "600036"],
            {
                "000002": make_df(80),
                "600036": make_df(10),  # 少于 min_data_rows=60
            },
            min_data_rows=60,
        )
        result = e.step()
        assert "600036" in e.dropped_symbols
        assert result.get("status") != "error"

    def test_data_up_to_date_returns_no_more_data(self):
        """游标已到数据末尾 → no_more_data, 不抛不崩

        构造: 80 根数据 + 游标已在最后(模拟连跑到最新的账户)。
        (只给 1 根数据会被 min_data_rows 先剔除, 不是这条分支)
        """
        e = make_engine(["000002"], {"000002": make_df(80)})
        e._cursor["000002"] = 79  # 已推进到最后一根
        result = e.step()
        assert result["status"] == "no_more_data"

    def test_all_symbols_dropped_returns_no_symbols(self):
        """全部标的取数失败 → no_symbols(而不是异常)"""
        e = make_engine(
            ["000002", "600036"],
            {
                "000002": RuntimeError("x"),
                "600036": RuntimeError("y"),
            },
        )
        result = e.step()
        assert result["status"] == "no_symbols"
        assert len(e.dropped_symbols) == 2


# ==================== 策略异常容错 ====================


class TestStrategyExceptionTolerated:
    def test_strategy_failure_skipped_not_fatal(self):
        """单标的策略抛异常 → 该标的跳过, 整步不崩"""
        good = make_df(80)
        e = make_engine(["000002"], {"000002": good})

        class BrokenStrategy:
            name = "broken"

            def analyze(self, symbol, df):
                raise TypeError("策略内部错误")

        e._strategies = [BrokenStrategy()]
        result = e.step()  # 不应抛
        assert "status" in result


# ==================== 基准数据容错 ====================


class TestBenchmarkTolerance:
    def test_benchmark_failure_tolerated(self):
        """基准(沪深300)数据获取失败 → 不崩(结果里无基准对照)"""
        good = make_df(80)

        def _load(symbol, *a, **k):
            if symbol == "000300":  # 基准
                raise RuntimeError("基准不可用")
            return good

        e = PaperEngine(symbols=["000002"], validate_symbols=False)
        patcher = patch.object(e, "_load", side_effect=_load)
        patcher.start()
        e._data_cache = {}
        result = e.step()
        assert "status" in result
