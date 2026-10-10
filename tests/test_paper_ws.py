"""模拟盘 WebSocket 推送测试（TD-06）

范围与诚实边界
--------------
推送的是**"有变化"信号**, 不是数据本身 —— 前端收到后调用既有 REST 刷新
(复用全部渲染逻辑, 避免快照同步问题)。A 股收盘后定时推进, 没有实时 ticks 可推;
真正的价值是: 内置调度 15:30 自动跑完后, 开着的页面**立即**看到结果。

全部离线(FastAPI TestClient 的 websocket 会话), 不依赖真实网络。
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from stock_model.paper.scheduler import PaperScheduler
from stock_model.web.app import create_app


def _app_and_scheduler():
    app = create_app()
    # app 启动时会 configure_paper_scheduler -> get_scheduler 单例
    from stock_model.paper.scheduler import get_scheduler

    return app, get_scheduler()


def _ws_testclient_broken() -> bool:
    """本机 starlette 1.0.1 + websockets 17.2 的 TestClient WS 传输不可用

    最小复现(与本项目代码无关):
        FastAPI 应用 + /ws 端点 + TestClient.websocket_connect → 连接即断
    同一组合在 CI 是绿的(CI 的依赖解析不同), 本机是坏的。
    这是第三方兼容问题, 不在本 PR 修 —— 端到端验证走真实浏览器
    (experiments/verify_paper_ui.py, Playwright 连真 WS)。
    判定方式: 连接收到 close 而非 welcome。
    """
    from fastapi import FastAPI, WebSocket
    from fastapi.testclient import TestClient

    probe = FastAPI()

    @probe.websocket("/probe")
    async def _probe(ws: WebSocket):
        await ws.accept()
        await ws.send_json({"ok": 1})
        while True:
            await ws.receive_text()

    try:
        with TestClient(probe) as c, c.websocket_connect("/probe") as w:
            return w.receive_text() != '{"ok":1}'
    except Exception:
        return True


WS_TESTCLIENT_OK = not _ws_testclient_broken()


@pytest.mark.skipif(
    not WS_TESTCLIENT_OK,
    reason="本机 starlette+websockets 的 TestClient WS 传输不可用(第三方兼容)",
)
class TestWsEndpoint:
    """单连接顺序验证(一条连接覆盖全部场景)

    调度器是进程级单例: 多个 TestClient 各自 create_app 会把单例的
    change_listener 绑到不同(已关闭的)事件循环上, 造成测试间串扰。
    真实使用也是"一个页面一条连接", 顺序化更贴近真实。
    """

    def test_full_lifecycle(self):
        app, sched = _app_and_scheduler()
        with TestClient(app) as client:
            with client.websocket_connect("/ws/paper") as ws:
                welcome = json.loads(ws.receive_text())
                assert welcome["type"] == "paper_status_changed"
                assert welcome["note"] == "connected"

                from stock_model.paper.scheduler import ScheduleState

                if "default" not in sched._states:
                    sched._states["default"] = ScheduleState(account_id="default")

                # ok 广播
                sched.set_runner(
                    lambda a, d: {"status": "ok", "steps": 1, "persisted": True, "trades": 0}
                )
                assert sched.run_now("default")["status"] == "ok"
                msg = json.loads(ws.receive_text())
                assert msg == {
                    "type": "paper_status_changed",
                    "account_id": "default",
                    "status": "ok",
                }

                # error 广播(含错误信息)
                def boom(a, d):
                    raise RuntimeError("x")

                sched.set_runner(boom)
                assert sched.run_now("default")["status"] == "error"
                msg = json.loads(ws.receive_text())
                assert msg["status"] == "error" and "x" in msg.get("error", "")

                # skipped 广播
                sched._notify_change("default", "skipped")
                assert json.loads(ws.receive_text())["status"] == "skipped"

        # 断开后: 无监听者时变更不能崩(客户端列表已被 handle 的 finally 清空)
        sched._notify_change("default", "ok")

    def test_scheduler_has_change_listener_hook(self, tmp_path):
        """钩子存在且可注入(None=清空) —— 与 set_alerter 同模式"""
        sched = PaperScheduler(schedule_file=tmp_path / "s.json", holiday_file=tmp_path / "h.json")
        calls: list[tuple] = []
        sched.set_change_listener(lambda account_id, status: calls.append((account_id, status)))
        sched._notify_change("default", "ok")
        assert calls == [("default", "ok")]
        sched.set_change_listener(None)
        sched._notify_change("default", "ok")  # 不应崩
        assert calls == [("default", "ok")]
        sched.shutdown()
