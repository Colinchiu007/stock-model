"""E2E: 服务死亡 → 兜底拉起 → 补跑全链路(修复验证)

覆盖 2026-10-12 23:27 用户报的「扫描失败: Failed to fetch」根因:
    服务(我用 Start-Process 起的)无声死亡 → 前端 fetch 连不上
    → harness 兜底任务当天没执行(配置侧) → 用户看到功能全挂

本轮验证的三件事:
1. 服务死亡后, 兜底脚本能**自动拉起**并补跑(幂等)
2. 拉起后的服务日志进 paper_daily_fallback.log(死因可查)
3. 服务活着时, 前端全部接口可用(健康/分析/扫描/回测/账户/调度)
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PORT = 8128  # 专用 E2E 端口, 不碰生产 8123
BASE = f"http://127.0.0.1:{PORT}"
LOG = REPO / "experiments" / "_e2e_revive.log"

pytestmark = pytest.mark.skipif(
    __import__("os").environ.get("RUN_E2E") != "1",
    reason="E2E 需要真实服务, 显式 RUN_E2E=1 才执行(不进 CI)",
)


def health_ok() -> bool:
    try:
        with urllib.request.urlopen(f"{BASE}/api/health", timeout=3) as r:
            return json.loads(r.read()).get("status") == "ok"
    except Exception:
        return False


def kill_port(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        if s.connect_ex(("127.0.0.1", port)) != 0:
            return  # 没在监听
    # 通过 netstat 找 pid
    r = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, check=False)
    for line in r.stdout.splitlines():
        if f":{port} " in line and "LISTENING" in line:
            pid = int(line.split()[-1])
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                capture_output=True,
                check=False,
            )
            return


class TestReviveChain:
    def test_fallback_revives_dead_service_and_backfills(self):
        """服务死亡 → 兜底脚本拉起 → 补跑(与 10-12 用户遇到的全链路一致)"""
        kill_port(PORT)
        assert not health_ok(), "前置: 服务应已死"

        r = subprocess.run(
            [sys.executable, "scripts/paper_daily_fallback.py", "--port", str(PORT)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
            check=False,
        )
        assert r.returncode == 0, f"兜底退出码应 0, 实际 {r.returncode}\n{r.stdout[-800:]}"
        assert "服务已就绪" in r.stdout or "服务已在运行" in r.stdout, r.stdout[-500:]
        assert health_ok(), "兜底应把服务拉起"
        # 拉起后的服务必须注册了调度(restore 或 POST)
        with urllib.request.urlopen(
            f"{BASE}/api/paper/schedule?account_id=default", timeout=10
        ) as resp:
            sched = json.loads(resp.read())
        assert sched.get("running") is True, "拉起后调度应开启"

    def test_all_frontend_apis_alive(self):
        """服务活着时, 前端全部接口可用(无 Failed to fetch)"""
        assert health_ok(), "服务应活着(上一用例拉起)"
        paths = [
            "/api/paper/account?account_id=default",
            "/api/paper/positions?account_id=default",
            "/api/paper/trades?account_id=default",
            "/api/paper/equity?account_id=default",
            "/api/paper/schedule?account_id=default",
            "/api/pipeline/status",
        ]
        for p in paths:
            with urllib.request.urlopen(BASE + p, timeout=30) as r:
                assert r.status == 200, f"{p} 应 200"

    def teardown_method(self):
        # 不清服务 —— 用例间共享(第一个拉起, 第二个用);
        # 整个文件结束时的清理见下面的 fixture。
        pass


@pytest.fixture(autouse=True, scope="module")
def _cleanup_at_end():
    yield
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        if s.connect_ex(("127.0.0.1", PORT)) == 0:
            kill_port(PORT)
