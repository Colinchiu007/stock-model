"""死信开关 ping 的单元测试（全离线：不访问任何真实 URL）

为什么值得测
----------
ping 是"机器彻底关机时唯一能通知到人的机制"的客户端半边。它的错误行为
方向是不对称的:
  - ping 失败被吞掉且不打日志 → 监控服务收不到 → 超时误报(可接受, 但要知道)
  - ping 失败抛异常 → **兜底脚本退出 1 → 外部调度器以为账没补 → 重试/告警**
    (不可接受: 监控挂了不能把账补上的事也搞坏)
  - 未配置时打印噪音 → 每天一条无用日志
所以锁: 正常送达 / /fail 后缀 / 未配置跳过 / 网络失败不抛且打日志。
"""

from __future__ import annotations

import importlib.util
import urllib.error
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def wd():
    path = REPO_ROOT / "scripts" / "watchdog_ping.py"
    assert path.is_file(), f"缺少脚本 {path}"
    spec = importlib.util.spec_from_file_location("_watchdog_ping", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestPing:
    def test_unconfigured_returns_none_and_is_silent(self, wd, capsys):
        """未配置 = 完全静默跳过(不给日志添噪), 返回 None 供调用方区分"""
        assert wd.ping_quiet(None) is None
        assert "未配置" in capsys.readouterr().out
        assert wd.ping_quiet("") is None

    def test_fail_uses_fail_suffix(self, wd, monkeypatch):
        """失败时 ping /fail —— 让监控服务**立即**告警, 而不是等超时"""
        captured = {}

        def fake_open(req, timeout=0):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()

            class R:
                status = 200

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

            return R()

        monkeypatch.setattr(wd.urllib.request, "urlopen", fake_open)
        assert wd.ping("https://hc-ping.com/abc", ok=False) is True
        assert captured["url"] == "https://hc-ping.com/abc/fail"
        assert captured["method"] == "GET"

    def test_ok_hits_base_url(self, wd, monkeypatch):
        captured = {}

        def fake_open(req, timeout=0):
            captured["url"] = req.full_url

            class R:
                status = 200

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

            return R()

        monkeypatch.setattr(wd.urllib.request, "urlopen", fake_open)
        assert wd.ping("https://hc-ping.com/abc/") is True  # 尾部斜杠也该正确
        assert captured["url"] == "https://hc-ping.com/abc"

    def test_network_error_does_not_raise_and_returns_false(self, wd, monkeypatch, capsys):
        """**最关键的一条**: 网络失败必须被吞掉并打日志

        否则"监控服务挂了"会让兜底脚本退出 1, 外部调度器误以为账没补。
        """

        def fake_open(req, timeout=0):
            raise urllib.error.URLError("监控服务不可达")

        monkeypatch.setattr(wd.urllib.request, "urlopen", fake_open)
        assert wd.ping("https://hc-ping.com/abc") is False  # 不抛
        assert "ping 失败" in capsys.readouterr().out  # 但要留痕

    def test_http_error_does_not_raise(self, wd, monkeypatch):
        """4xx/5xx 也是失败路径(urllib 会抛 HTTPError, 它是 URLError 的子类)"""

        def fake_open(req, timeout=0):
            raise urllib.error.HTTPError("https://x", 500, "boom", None, None)

        monkeypatch.setattr(wd.urllib.request, "urlopen", fake_open)
        assert wd.ping("https://hc-ping.com/abc") is False


class TestImportSafety:
    def test_no_module_side_effects(self):
        import warnings

        spec = importlib.util.spec_from_file_location(
            "_wd_side_effect_probe", REPO_ROOT / "scripts" / "watchdog_ping.py"
        )
        module = importlib.util.module_from_spec(spec)
        before = warnings.filters[:]
        spec.loader.exec_module(module)
        assert warnings.filters == before
