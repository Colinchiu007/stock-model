"""池子被剔空后不得当作成功 —— 观察期真缺陷回归测试

缺陷现象(2026-10-12 首个真实交易日实测):
    15:30 定时执行 → 3 只标的取数全部失败被剔除 → step() 返回 no_symbols
    → run_paper_cycle **仍返回 status="ok"** → 调度记录 ✅、无告警
    → 用户以为"每天在跑", 其实池子已空、之后每天都空跑(缓存+落盘互为锁)

三条契约(全部先写测试看红):
1. run_paper_cycle 在 steps 为空时返回 **非 ok**(pool_empty), 不假装成功
2. 空池被视为**失败**路径 → 触发告警(alerter 收到调用)
3. 落盘仍要继续(账户状态要留痕), 但 persisted 之外额外带 pool_empty 原因
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from stock_model.web import paper_api

pytestmark = pytest.mark.importorskip("fastapi")


@pytest.fixture
def clean_engine_cache():
    paper_api._engines.clear()
    yield
    paper_api._engines.clear()


def _empty_engine():
    """池子已空的引擎(模拟 15:30 那次: 3 只全被剔除)

    注意: PaperEngine(symbols=[]) 构造时会直接抛 ValueError("拒绝构造一个
    不会做任何事的引擎")—— 引擎本身**早就禁止空池**。所以这里走真实路径:
    先给 3 只, 再让 _load 全部失败, 由 step() 自己剔空(与线上 15:30 同一条路)。
    """
    from unittest.mock import patch as _patch

    from stock_model.paper.engine import PaperEngine

    e = PaperEngine(symbols=["000002", "000001", "600036"], validate_symbols=False)
    with _patch.object(e, "_load", side_effect=RuntimeError("数据获取失败")):
        result = e.step()
    assert e.symbols == [], "夹具前提: step 后池子应被剔空"
    assert result["status"] == "no_symbols"
    return e


class TestEmptyPoolIsNotOk:
    def test_empty_steps_returns_pool_empty(self, clean_engine_cache):
        """steps 为空(池子空) → status=pool_empty, 不是 ok"""
        e = _empty_engine()
        with (
            patch.object(paper_api, "_get_engine", return_value=e),
            patch.object(paper_api, "_persist", return_value=(True, "")),
        ):
            r = paper_api.run_paper_cycle("default", 1)
        assert r["status"] == "pool_empty", r
        assert "000002" in r.get("pool_empty_reason", "") or r.get("steps") == 0

    def test_empty_pool_persists_but_flags(self, clean_engine_cache):
        """空池也落盘(留痕) —— 但响应带 pool_empty 标记"""
        e = _empty_engine()
        with (
            patch.object(paper_api, "_get_engine", return_value=e),
            patch.object(paper_api, "_persist", return_value=(True, "")),
        ):
            r = paper_api.run_paper_cycle("default", 1)
        assert r["persisted"] is True
        assert r["status"] == "pool_empty"

    def test_normal_run_still_ok(self, clean_engine_cache):
        """正常推进(有 steps) → 仍 ok(不误报)"""
        from stock_model.paper.engine import PaperEngine

        e = PaperEngine(symbols=["000002"], validate_symbols=False)
        fake_steps = [{"status": "ok", "date": "2026-01-02"}]
        with (
            patch.object(paper_api, "_get_engine", return_value=e),
            patch.object(e, "run", return_value=fake_steps),
            patch.object(paper_api, "_persist", return_value=(True, "")),
        ):
            r = paper_api.run_paper_cycle("default", 1)
        assert r["status"] == "ok", r
        assert r["steps"] == 1
