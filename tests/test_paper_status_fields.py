"""修复 paper_status.py 的字段名 bug —— 成交数永远显示 0

bug: 2026-10-13 实测, 账户实际 2 笔成交, 但状态卡显示"成交 0 笔"。
根因: paper_status.py 从 /api/paper/account 读 total_trades/trades,
但该接口**没有**这两个字段(只有 cash/total_asset/trading_days 等)。
真实成交数要从 /api/paper/trades 拿。

契约:
1. 成交数 = len(/api/paper/trades 返回的 trades)
2. 交易日 = /api/paper/account 的 trading_days
3. 两个接口任一失败 → 降级显示(不是崩)
"""

from __future__ import annotations

import importlib.util
import json
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "_paper_status", REPO / "scripts" / "paper_status.py"
    )
    m = importlib.util.module_from_spec(spec)
    sys.modules["_paper_status"] = m
    spec.loader.exec_module(m)
    return m


class FakeResp:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_trades_count_comes_from_trades_endpoint(monkeypatch):
    """成交数必须来自 /api/paper/trades(不是 account 的臆测字段)"""
    m = _load_module()
    monkeypatch.setattr(sys, "argv", ["paper_status.py", "8123"])
    calls: list[str] = []

    def fake_urlopen(req, timeout=10):
        path = req.full_url if hasattr(req, "full_url") else str(req)
        calls.append(path)
        if "/api/paper/trades" in path:
            return FakeResp({"trades": [{"id": "T1"}, {"id": "T2"}], "total": 2})
        if "/api/paper/account" in path:
            return FakeResp(
                {
                    "total_asset": 9984.98,
                    "cash": 8384.98,
                    "total_return": -0.0015,
                    "trading_days": 61,
                }
            )
        return FakeResp({"status": "ok"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    code = m.main()
    assert code == 0
    assert any("/api/paper/trades" in c for c in calls), "必须请求 trades 接口"
    # 函数级断言: 成交数解析为 2
    assert m._trades_count(8123) == 2


def test_account_without_trades_field_does_not_break(monkeypatch, capsys):
    """account 接口没有 trades 字段也不能崩(降级为 0)"""

    def fake_urlopen(req, timeout=10):
        path = req.full_url if hasattr(req, "full_url") else str(req)
        if "/api/paper/trades" in path:
            raise OSError("trades 接口不可用")
        if "/api/paper/account" in path:
            return FakeResp(
                {
                    "total_asset": 9984.98,
                    "cash": 8384.98,
                    "total_return": -0.0015,
                    "trading_days": 61,
                }
            )
        return FakeResp({"status": "ok"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    m = _load_module()
    monkeypatch.setattr(sys, "argv", ["paper_status.py", "8123"])
    code = m.main()
    out = capsys.readouterr().out
    assert code == 0
    assert "成交 0 笔" in out  # 降级显示, 不崩
