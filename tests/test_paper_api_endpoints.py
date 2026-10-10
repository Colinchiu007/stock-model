"""模拟盘 API 端点测试(含"重启后状态还在"的端到端验证)

与 ``test_paper_persistence.py`` 的分工:
  - 那边测内部接线(离线可跑, CI 主 job 也跑)
  - 这边测**真实 HTTP 路径**, 需要 fastapi, 故整体 importorskip

「重启」在这里怎么模拟
----------------------
引擎表 ``paper_api._engines`` 是**模块级**的, 所以新建一个 app 并不等于重启。
真正的重启语义是"进程内存全没了、只剩磁盘", 因此这里显式清空 ``_engines``。
"""

from __future__ import annotations

import builtins
import sys
from contextlib import contextmanager
from unittest.mock import patch

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

import stock_model.paper.scheduler as sched_mod
from stock_model.paper import store
from stock_model.web import paper_api
from stock_model.web.app import create_app
from tests.test_paper_persistence import _FakeFetcher


@contextmanager
def _block_apscheduler():
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("apscheduler"):
            raise ImportError("No module named 'apscheduler'")
        return real_import(name, *args, **kwargs)

    for mod in ("apscheduler", "apscheduler.schedulers", "apscheduler.schedulers.background"):
        sys.modules.pop(mod, None)
    with patch("builtins.__import__", fake_import):
        yield


@pytest.fixture
def client(tmp_path, monkeypatch):
    """隔离的 app + 隔离的落盘目录 + 隔离的调度配置"""
    monkeypatch.setattr(store, "DEFAULT_STORE_DIR", tmp_path / "paper")
    monkeypatch.setattr("stock_model.data.fetcher.StockDataFetcher", _FakeFetcher)
    paper_api._engines.clear()
    paper_api._locks.clear()

    isolated = sched_mod.PaperScheduler(
        schedule_file=tmp_path / "schedule.json",
        holiday_file=tmp_path / "holidays.json",
    )
    monkeypatch.setattr(sched_mod, "_scheduler_singleton", isolated)

    with TestClient(create_app()) as c:
        yield c

    isolated.shutdown()
    paper_api._engines.clear()
    paper_api._locks.clear()


def _restart() -> None:
    """模拟进程重启: 内存里的引擎全丢, 只剩磁盘"""
    paper_api._engines.clear()


class TestPaperEndpoints:
    def test_account_overview(self, client):
        resp = client.get("/api/paper/account", params={"account_id": "acc"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["initial_capital"] == pytest.approx(10000.0)
        assert body["disclaimer"], "模拟盘响应必须带免责声明"

    def test_step_reports_persisted(self, client):
        resp = client.post("/api/paper/step", json={"account_id": "acc"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["persisted"] is True, f"推进后必须落盘: {body}"

    def test_run_advances_days(self, client):
        resp = client.post("/api/paper/run", json={"account_id": "acc", "days": 4})
        body = resp.json()
        assert body["status"] == "ok"
        assert body["steps"] == 4
        assert body["persisted"] is True

    def test_trades_and_positions_shapes(self, client):
        assert client.get("/api/paper/trades").json()["total"] == 0
        assert client.get("/api/paper/positions").json()["count"] == 0
        assert client.get("/api/paper/equity").json()["count"] == 0


class TestRestartOverHTTP:
    """端到端: 跑一段 → 停服(清内存) → 重启 → 查成交, 记录应该还在"""

    def test_trades_survive_restart(self, client):
        from stock_model.paper.models import Side, Trade

        client.post("/api/paper/run", json={"account_id": "acc", "days": 3})
        engine = paper_api._get_engine("acc")
        engine.account.trades.append(
            Trade("T-9", "000002", Side.BUY, 100, 10.0, 1000.0, executed_at="2024-02-01")
        )
        paper_api._persist("acc", engine)

        equity_before = client.get("/api/paper/equity", params={"account_id": "acc"}).json()
        assert equity_before["count"] > 0

        _restart()

        trades = client.get("/api/paper/trades", params={"account_id": "acc"}).json()
        assert trades["total"] == 1, "重启后成交记录丢失"
        assert trades["trades"][0]["trade_id"] == "T-9"

        equity_after = client.get("/api/paper/equity", params={"account_id": "acc"}).json()
        assert equity_after["count"] == equity_before["count"], "重启后资金曲线丢失"

    def test_restart_continues_forward(self, client):
        """重启后再推进必须是往前走, 不能回到开头重放"""
        first = client.post("/api/paper/run", json={"account_id": "acc", "days": 3}).json()
        _restart()
        second = client.post("/api/paper/run", json={"account_id": "acc", "days": 2}).json()

        assert second["from"] > first["to"], (
            f"重启后从 {second['from']} 开始, 但重启前已推进到 {first['to']} —— 历史被重放"
        )

    def test_account_id_isolated(self, client):
        """两个账户互不覆盖(engine 曾把 account_id 硬编码成 paper-default)"""
        client.post("/api/paper/run", json={"account_id": "alpha", "days": 2})
        client.post("/api/paper/run", json={"account_id": "beta", "days": 5})

        assert client.get("/api/paper/equity", params={"account_id": "alpha"}).json()["count"] == 2
        assert client.get("/api/paper/equity", params={"account_id": "beta"}).json()["count"] == 5
        assert set(store.list_accounts(store.DEFAULT_STORE_DIR)) == {"alpha", "beta"}

    def test_reset_clears_disk_state(self, client):
        client.post("/api/paper/run", json={"account_id": "acc", "days": 3})
        resp = client.post("/api/paper/reset", json={"account_id": "acc"})
        assert resp.json()["state_file_removed"] is True

        _restart()
        assert client.get("/api/paper/equity", params={"account_id": "acc"}).json()["count"] == 0


class TestScheduleEndpoints:
    def test_start_status_stop(self, client):
        pytest.importorskip("apscheduler")

        started = client.post("/api/paper/schedule", json={"hour": 15, "minute": 30, "days": 1})
        assert started.status_code == 200, started.text
        body = started.json()
        assert body["running"] is True
        assert body["trigger_type"] == "daily"

        status = client.get("/api/paper/schedule", params={"account_id": "default"}).json()
        assert status["running"] is True
        assert status["next_run_time"]

        stopped = client.delete("/api/paper/schedule", params={"account_id": "default"})
        assert stopped.status_code == 200
        assert (
            client.get("/api/paper/schedule", params={"account_id": "default"}).json()["running"]
            is False
        )

    def test_status_exposes_alert_channel(self, client):
        """HTTP 层必须回显告警通道 —— 否则用户以为"失败会通知我"

        这条同时验证装配路径: ``create_app`` 与端点都走
        ``configure_paper_scheduler()``, 漏配任何一处都会让告警在某条路径上静默失效。
        不断言具体通道名(取决于开发者本地 .env), 只要求字段在场且非空。
        """
        pytest.importorskip("apscheduler")
        client.post("/api/paper/schedule", json={"account_id": "acc", "hour": 15, "minute": 30})

        status = client.get("/api/paper/schedule", params={"account_id": "acc"}).json()
        assert status["alert_channel"], "status 必须回显告警通道(或'未接入')"
        for key in ("alert_count", "last_alert_at", "last_alert_error"):
            assert key in status, f"status 缺少告警字段 {key}"

    def test_missing_dependency_returns_503(self, client):
        """缺 apscheduler 必须返回 503, 不能 200 + 一个错误字段

        与 /api/pipeline/start 的约定一致: 让调用方知道"没跑起来"。
        """
        with _block_apscheduler():
            resp = client.post("/api/paper/schedule", json={"hour": 15, "minute": 30})

        assert resp.status_code == 503, f"应为 503, 实际 {resp.status_code}: {resp.text[:200]}"
        assert "apscheduler" in resp.text.lower()

    def test_invalid_params_return_400(self, client):
        pytest.importorskip("apscheduler")
        resp = client.post("/api/paper/schedule", json={"hour": 99})
        assert resp.status_code == 400

    def test_run_now_goes_through_same_path(self, client, monkeypatch):
        """「立即执行」与定时触发共用一条路径 —— 手动跑得通, 定时才跑得通"""
        pytest.importorskip("apscheduler")
        # 今天可能是周末, 显式让它判定为交易日以保持测试确定性
        monkeypatch.setattr(sched_mod, "is_trading_day", lambda day, holidays=None: True)

        client.post("/api/paper/schedule", json={"account_id": "acc", "days": 1})
        resp = client.post("/api/paper/schedule/run", json={"account_id": "acc"})

        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "ok"
        assert (
            client.get("/api/paper/account", params={"account_id": "acc"}).json()["trading_days"]
            >= 1
        )

    def test_run_now_reports_failure_not_200_ok(self, client, monkeypatch):
        """定时执行失败时, 响应本身就要报告失败"""
        pytest.importorskip("apscheduler")
        monkeypatch.setattr(sched_mod, "is_trading_day", lambda day, holidays=None: True)

        def boom(account_id, days):
            raise RuntimeError("数据源挂了")

        monkeypatch.setattr(paper_api, "run_paper_cycle", boom)
        sched = sched_mod.get_scheduler()
        sched.set_runner(boom)
        sched.start("acc", hour=15, minute=30)

        body = client.post("/api/paper/schedule/run", json={"account_id": "acc"}).json()
        assert body["status"] == "error"
        assert "数据源挂了" in body["error"]

        status = client.get("/api/paper/schedule", params={"account_id": "acc"}).json()
        assert status["last_status"] == "error"
        assert status["error_count"] == 1
