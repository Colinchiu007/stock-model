"""引擎游标不变量: cursor 必须等于已记录的资金曲线点数

为什么需要
---------
2026-10-18 手工修复账户数据时, 把资金曲线截断到 65 点, 但把 ``cursor``
设成了 64 —— 差一。后果是 ``step()`` 会**重复处理最后一根 bar**:
隔离演练实测 ``step()`` 返回 ``date=2024-04-12``, 与曲线末点日期重复。

这个错误没有任何现有测试能发现(它们要么不跑 step, 要么用真实数据),
只有"拿真实状态在隔离目录里跑一遍"才暴露。故在此锁死:

    cursor[symbol] == len(equity_curve)

语义: ``cursor`` 是"已处理到的 bar 序号", ``equity_curve`` 每成功推进一步
记一个点, 两者必须同进同退。
"""

from __future__ import annotations

import pandas as pd
import pytest

from stock_model.paper.engine import PaperEngine


def _fake_bars(n: int = 100) -> pd.DataFrame:
    """合成日线: 缓涨、无极端信号, 足够跑通策略与撮合"""
    idx = pd.bdate_range("2024-01-01", periods=n)
    close = [10.0 + i * 0.01 for i in range(n)]
    return pd.DataFrame(
        {
            "open": close,
            "high": [c * 1.01 for c in close],
            "low": [c * 0.99 for c in close],
            "close": close,
            "volume": [1_000_000] * n,
        },
        index=idx,
    )


def _engine() -> PaperEngine:
    engine = PaperEngine(symbols=["000002"], validate_symbols=False)
    engine._data_cache = {"000002": _fake_bars()}
    return engine


class TestCursorCurveInvariant:
    def test_cursor_equals_curve_length_after_each_step(self):
        """每一步之后 cursor 都等于曲线点数(差一会导致重复推进)"""
        engine = _engine()
        for i in range(5):
            result = engine.step()
            if result.get("status") != "ok":
                pytest.skip(f"合成数据第 {i} 步未推进: {result.get('status')}")
            cursor = engine.state_dict()["cursor"]["000002"]
            curve_len = len(engine.account.equity_curve)
            assert cursor == curve_len, (
                f"第 {i + 1} 步后 cursor={cursor} != 曲线点数={curve_len} "
                f"(差一 -> 下一步会重复处理同一根 bar)"
            )

    def test_curve_dates_strictly_increasing(self):
        """曲线日期必须严格递增, 不得重复/回退"""
        engine = _engine()
        for _ in range(5):
            if engine.step().get("status") != "ok":
                break
        dates = [e.date for e in engine.account.equity_curve]
        assert dates == sorted(dates), f"日期非递增: {dates}"
        assert len(dates) == len(set(dates)), f"日期有重复: {dates}"

    def test_restored_cursor_drives_next_bar(self):
        """恢复游标后, 下一步必须落在"曲线的下一根", 而不是重复末点"""
        engine = _engine()
        engine.step()
        engine.step()
        state = engine.state_dict()
        last_date = engine.account.equity_curve[-1].date

        # 用落盘状态重建引擎(模拟服务重启后的恢复路径)
        restored = PaperEngine(symbols=["000002"], validate_symbols=False)
        restored._data_cache = {"000002": _fake_bars()}
        restored.load_state(state)

        result = restored.step()
        if result.get("status") != "ok":
            pytest.skip(f"恢复后未推进: {result.get('status')}")
        assert str(result.get("date")) > str(last_date), (
            f"恢复后推进到 {result.get('date')}, 但曲线末点已是 {last_date} —— "
            f"说明 cursor 差一, 正在重复处理"
        )
