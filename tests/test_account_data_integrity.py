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

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]


def load_default() -> dict:
    f = REPO / "data" / "paper" / "default.json"
    if not f.exists():  # pragma: no cover - 环境无账户文件时跳过
        pytest.skip("无账户文件")
    return json.loads(f.read_text(encoding="utf-8"))


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
            spent += (float(t.get("commission") or 0.0)
                      + float(t.get("stamp_tax") or 0.0)
                      + float(t.get("transfer_fee") or 0.0))
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
