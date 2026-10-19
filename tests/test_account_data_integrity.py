"""回归保护: 账户数据完整性不变量(观察期 2026-10-18 修复后锁)

背景(为什么要有这些锁):
  E2E 服务(8127/8128)曾与生产共享 data/paper/, 补跑写进了生产账户:
  - 产生第二笔成交, trade_id 与第一笔**重复**(两个实例各自从 0 计数)
  - 10-12 空跑时的 dropped_symbols 残留未随后续成功运行清空
  两者都是"数据看起来正常但统计会错"的类型 —— 正是本项目最怕的。

契约(任何一次账户写入/恢复后都必须成立):
1. trade_id 唯一(重复会破坏按 ID 的追踪与统计)
2. 现金 = 初始资金 - Σ(成交金额 + 各项费用)(不含被删交易的残留)
3. 持仓标的 ⊆ 成交涉及标的(有持仓必有对应成交)
4. dropped_symbols 只应包含**本次运行**被剔除的标的,
   成功推进后不得残留历史 dropped(否则显示/诊断误导)
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]


def load_default() -> dict[str, Any]:
    f = REPO / "data" / "paper" / "default.json"
    if not f.exists():  # pragma: no cover - 环境无账户文件时跳过
        pytest.skip("无账户文件")
    data: dict[str, Any] = json.loads(f.read_text(encoding="utf-8"))
    return data


class TestAccountDataIntegrity:
    def test_trade_ids_unique(self):
        """trade_id 必须唯一 —— 曾因多实例各自计数出现重复"""
        d = load_default()
        ids = [t.get("trade_id") for t in (d.get("trades") or [])]
        assert len(ids) == len(set(ids)), f"trade_id 重复: {ids}"

    def test_cash_matches_trades(self):
        """现金 = 初始 - Σ(成交金额 + 费用)"""
        d = load_default()
        spent = 0.0
        for t in d.get("trades") or []:
            spent += float(t.get("amount") or 0.0)
            spent += (
                float(t.get("commission") or 0.0)
                + float(t.get("stamp_tax") or 0.0)
                + float(t.get("transfer_fee") or 0.0)
            )
        expected = float(d.get("initial_capital") or 0.0) - spent
        assert float(d.get("cash")) == pytest.approx(expected, abs=0.01), (
            f"现金 {d.get('cash')} != 期望 {expected}"
        )

    def test_positions_have_matching_trades(self):
        """有持仓必须有对应成交(不能凭空出现/残留)"""
        d = load_default()
        traded = {t.get("symbol") for t in (d.get("trades") or [])}
        held = set((d.get("positions") or {}).keys())
        assert held <= traded, f"无成交却有持仓: {held - traded}"

    def test_dropped_symbols_not_stale(self):
        """dropped_symbols 不得与当前 symbols 重合(重合=残留的历史值)"""
        d = load_default()
        meta = d.get("_metadata") or {}
        dropped = set(meta.get("dropped_symbols") or [])
        symbols = set(meta.get("symbols") or [])
        overlap = dropped & symbols
        assert not overlap, (
            f"dropped_symbols 残留(与当前池重合): {overlap} —— "
            f"成功推进后应清空, 否则诊断/显示会误导"
        )

    def test_cursor_equals_curve_length(self):
        """cursor 必须等于资金曲线点数(差一会重复推进同一根 bar)

        2026-10-18 手工修复时把曲线截断到 65 点却把 cursor 设成 64 ——
        差一导致 step() 重复处理最后一根 bar(隔离演练实测返回重复日期)。
        """
        d = load_default()
        curve_len = len(d.get("equity_curve") or [])
        cursor = (d.get("_metadata") or {}).get("cursor") or {}
        if not cursor:  # 全新账户还没推进过
            pytest.skip("无游标")
        for symbol, idx in cursor.items():
            assert idx == curve_len, (
                f"{symbol} cursor={idx} != 曲线点数={curve_len} —— "
                f"差一会让下一步重复处理最后一根 bar"
            )

    def test_curve_dates_strictly_increasing(self):
        """曲线日期必须严格递增且不重复"""
        d = load_default()
        dates = [e.get("date") for e in (d.get("equity_curve") or [])]
        if not dates:
            pytest.skip("无曲线")
        assert dates == sorted(dates), f"曲线日期非递增(尾部): {dates[-5:]}"
        assert len(dates) == len(set(dates)), f"曲线日期有重复: {dates[-5:]}"

    def test_curve_last_point_self_consistent(self):
        """曲线末点: cash + market_value == total_asset"""
        d = load_default()
        curve = d.get("equity_curve") or []
        if not curve:
            pytest.skip("无曲线")
        last = curve[-1]
        total = float(last.get("cash") or 0) + float(last.get("market_value") or 0)
        assert abs(total - float(last.get("total_asset") or 0)) < 0.01, (
            f"末点不自洽: cash+市值={total:.2f} vs total_asset={last.get('total_asset')}"
        )
