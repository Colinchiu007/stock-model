"""回归: netstat 端口解析在中文 Windows 不能因解码崩溃

2026-10-13 E2E teardown 实测: kill_port 的 netstat 解析用 ``text=True``
默认按 UTF-8 解码中文 Windows 的 GBK 输出 → subprocess reader 线程炸
UnicodeDecodeError → ``r.stdout`` 永远为 None → kill_port AttributeError,
服务也没被杀掉(残留 8128 进程)。

本测试起真实子进程监听, 验证 ``_listener_pids`` 解析路径(只解析, 不杀进程)。
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time

import pytest

from tests.test_revive_chain_e2e import _listener_pids

# 被保护的 bug(GBK 输出 + netstat/taskkill 路径)只存在于中文 Windows;
# 其他平台 netstat 输出格式不同, 该解析路径本就不适用(CI 跑 ubuntu)。
pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="netstat -ano/taskkill 是 Windows 专用路径, 被保护的 bug 也只在中文 Windows 出现",
)

_CHILD_CODE = (
    "import socket, time; "
    "s = socket.socket(); "
    "s.bind(('127.0.0.1', {port})); "
    "s.listen(1); "
    "time.sleep(300)"
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class TestListenerPids:
    def test_finds_child_listener_pid(self):
        """真实子进程监听 → 解析必须返回它的 PID(解码修复的回归保护)"""
        port = _free_port()
        child = subprocess.Popen(
            [sys.executable, "-c", _CHILD_CODE.format(port=port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.time() + 10
            while time.time() < deadline:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(0.5)
                    if s.connect_ex(("127.0.0.1", port)) == 0:
                        break
                time.sleep(0.2)
            else:
                raise AssertionError(f"子进程监听 {port} 超时")

            # netstat 列表可见性可能有微小时延, 短轮询容忍
            pids: list[int] = []
            deadline = time.time() + 3
            while time.time() < deadline:
                pids = _listener_pids(port)
                if child.pid in pids:
                    break
                time.sleep(0.3)
            assert pids == [child.pid], f"应只找到子进程 {child.pid}, 实际 {pids}"
        finally:
            child.terminate()
            child.wait(timeout=10)

    def test_no_listener_returns_empty(self):
        """没人监听的端口 → 空列表(提前返回路径)"""
        port = _free_port()
        assert _listener_pids(port) == []
