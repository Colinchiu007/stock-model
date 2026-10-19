"""兜底脚本的账户自洽校验 —— 让"静默错数据"变成生产可见

背景
----
2026-10-19 的 ``cursor`` 差一(cursor=64 而曲线 65 点)会让 ``step()``
重复处理最后一根 bar, 策略在同一根 bar 上再跑一次。
但 HTTP 依然 200、``status`` 依然 ``ok``, ``run_exit_code()`` 看不出来 ——
只有读落盘数据验不变量才能发现。

兜底脚本是无人值守路径的最后一道, 故把校验放在那里:
失败返回非 0 -> 死信开关 ping /fail -> 告警。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def fb():
    scripts_dir = str(REPO_ROOT / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    path = REPO_ROOT / "scripts" / "paper_daily_fallback.py"
    spec = importlib.util.spec_from_file_location("_fb_invariants", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(tmp_path: Path, data: dict[str, Any]) -> None:
    (tmp_path / "default.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _ok_account() -> dict[str, Any]:
    """自洽账户: 2 个曲线点, cursor=2, 成交 ID 唯一"""
    return {
        "initial_capital": 10000.0,
        "cash": 9000.0,
        "positions": {},
        "trades": [{"trade_id": "T000001", "symbol": "000002", "amount": 1000.0}],
        "equity_curve": [
            {"date": "2024-01-02", "cash": 10000.0, "market_value": 0.0, "total_asset": 10000.0},
            {"date": "2024-01-03", "cash": 9000.0, "market_value": 1000.0, "total_asset": 10000.0},
        ],
        "_metadata": {"symbols": ["000002"], "cursor": {"000002": 2}},
    }


class TestVerifyAccountInvariants:
    def test_consistent_account_passes(self, fb, tmp_path, monkeypatch):
        monkeypatch.setenv("STOCK_PAPER_DIR", str(tmp_path))
        _write(tmp_path, _ok_account())
        assert fb.verify_account_invariants() == []

    def test_missing_file_is_not_failure(self, fb, tmp_path, monkeypatch):
        """全新环境还没账户文件, 不算失败"""
        monkeypatch.setenv("STOCK_PAPER_DIR", str(tmp_path))
        assert fb.verify_account_invariants() == []

    def test_cursor_off_by_one_detected(self, fb, tmp_path, monkeypatch):
        """cursor 与曲线点数差一 -> 必须报出来(2026-10-19 那个缺陷)"""
        monkeypatch.setenv("STOCK_PAPER_DIR", str(tmp_path))
        data = _ok_account()
        data["_metadata"]["cursor"] = {"000002": 1}  # 曲线 2 点, cursor 1
        _write(tmp_path, data)
        problems = fb.verify_account_invariants()
        assert any("cursor=1" in p and "曲线点数=2" in p for p in problems), problems

    def test_duplicate_curve_dates_detected(self, fb, tmp_path, monkeypatch):
        monkeypatch.setenv("STOCK_PAPER_DIR", str(tmp_path))
        data = _ok_account()
        data["equity_curve"][1]["date"] = data["equity_curve"][0]["date"]
        data["_metadata"]["cursor"] = {"000002": 2}
        _write(tmp_path, data)
        problems = fb.verify_account_invariants()
        assert any("重复" in p for p in problems), problems

    def test_non_increasing_curve_detected(self, fb, tmp_path, monkeypatch):
        monkeypatch.setenv("STOCK_PAPER_DIR", str(tmp_path))
        data = _ok_account()
        data["equity_curve"].reverse()  # 日期倒序
        data["_metadata"]["cursor"] = {"000002": 2}
        _write(tmp_path, data)
        problems = fb.verify_account_invariants()
        assert any("非递增" in p for p in problems), problems

    def test_duplicate_trade_id_detected(self, fb, tmp_path, monkeypatch):
        """多实例写同一账户会撞号 —— 曾真实发生(E2E 与生产共享目录)"""
        monkeypatch.setenv("STOCK_PAPER_DIR", str(tmp_path))
        data = _ok_account()
        data["trades"].append(dict(data["trades"][0]))
        _write(tmp_path, data)
        problems = fb.verify_account_invariants()
        assert any("trade_id 重复" in p for p in problems), problems

    def test_corrupt_file_reported(self, fb, tmp_path, monkeypatch):
        monkeypatch.setenv("STOCK_PAPER_DIR", str(tmp_path))
        (tmp_path / "default.json").write_text("{not json", encoding="utf-8")
        problems = fb.verify_account_invariants()
        assert problems and "读取失败" in problems[0]

    def test_main_fails_when_invariants_break(self, fb, tmp_path, monkeypatch):
        """不变量破了要让 main 返回非 0, 否则死信开关不会告警"""
        monkeypatch.setenv("STOCK_PAPER_DIR", str(tmp_path))
        data = _ok_account()
        data["_metadata"]["cursor"] = {"000002": 1}
        _write(tmp_path, data)

        monkeypatch.setattr(fb, "http_json", lambda *a, **k: {"status": "ok", "steps": 1})
        monkeypatch.setattr(fb, "server_alive", lambda: True)
        monkeypatch.setattr(fb, "today_handled", lambda *a, **k: False)
        monkeypatch.setattr(fb, "run_exit_code", lambda result: 0)
        monkeypatch.setattr(fb, "ping_quiet", lambda *a, **k: None)
        monkeypatch.setattr(sys, "argv", ["paper_daily_fallback.py"])
        assert fb.main() == 1
