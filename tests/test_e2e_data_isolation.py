"""E2E 数据污染修复: STOCK_PAPER_DIR 环境变量隔离数据目录

问题(2026-10-13 00:30 实测):
  test_revive_chain_e2e.py 的兜底在 8128 起服务, 数据目录 data/paper/
  与生产(8123)**共享** —— E2E 补跑写进了生产账户, 产生 trade_id 重复的
  第二笔成交(T000001 出现两次)。**测试污染了生产数据**。

修复: store 层支持 STOCK_PAPER_DIR 环境变量(默认仍 data/paper/)。
  - E2E 起服务时设 STOCK_PAPER_DIR=<临时目录> → 与生产隔离
  - 运维价值: 数据目录可迁走(备份盘/多账户), 不必动代码

契约:
1. 未设 STOCK_PAPER_DIR → 用默认 data/paper(现有行为不变)
2. 设了 → 用该目录(自动创建)
3. 兜底脚本/E2E 用该变量隔离
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

from tests.test_revive_chain_e2e import kill_port

REPO = Path(__file__).resolve().parents[1]
PORT = 8127
BASE = f"http://127.0.0.1:{PORT}"

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_E2E") != "1",
    reason="E2E 需要真实服务, 显式 RUN_E2E=1 才执行(不进 CI)",
)


class TestPaperDirEnv:
    def test_env_var_overrides_store_dir(self, tmp_path, monkeypatch):
        """STOCK_PAPER_DIR 必须改变 store 的落地目录"""
        monkeypatch.setenv("STOCK_PAPER_DIR", str(tmp_path / "paper"))
        # 重新导入以让环境变量生效(模块级缓存需清)
        import importlib

        from stock_model.paper import store as store_mod

        importlib.reload(store_mod)
        try:
            from stock_model.paper.models import Account

            account_id = "isolated_e2e_probe"
            acc = Account(account_id=account_id, initial_capital=1000.0, cash=1000.0)
            store_mod.save_account(acc)
            p = tmp_path / "paper" / f"{account_id}.json"
            assert p.exists(), f"账户应落到 {p}"
            # 默认目录绝不能被写(注意先清掉历史遗留的同名文件)
            default_p = REPO / "data" / "paper" / f"{account_id}.json"
            if default_p.exists():  # pragma: no cover - 仅历史遗留
                default_p.unlink()
            assert not default_p.exists()
        finally:
            importlib.reload(store_mod)

    def test_default_dir_unchanged_without_env(self, monkeypatch):
        """不设环境变量 → 仍是默认 data/paper(现有行为)"""
        monkeypatch.delenv("STOCK_PAPER_DIR", raising=False)
        import importlib

        from stock_model.paper import store as store_mod

        importlib.reload(store_mod)
        try:
            assert store_mod.DEFAULT_STORE_DIR == REPO / "data" / "paper"
        finally:
            importlib.reload(store_mod)


class TestE2EIsolation:
    def test_revive_e2e_does_not_touch_production(self, tmp_path):
        """复活链 E2E 在隔离数据目录中跑, 生产 default.json 字节数不变"""
        # 前置: 端口必须干净 —— 否则兜底会复用**旧服务**(它没有隔离目录的
        # 环境变量), 测试就失去意义(2026-10-13 实测踩过)。
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            assert s.connect_ex(("127.0.0.1", PORT)) != 0, (
                f"端口 {PORT} 被占用 —— 会连到旧服务, 隔离断言不可信"
            )

        prod = REPO / "data" / "paper" / "default.json"
        before = prod.read_bytes() if prod.exists() else b""

        iso = tmp_path / "e2e_paper"
        # 用兜底脚本起服务, 数据目录隔离
        env = {**os.environ, "STOCK_PAPER_DIR": str(iso), "RUN_E2E": "1"}
        r = subprocess.run(
            [sys.executable, "scripts/paper_daily_fallback.py", "--port", str(PORT)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
            env=env,
            check=False,
        )
        assert r.returncode == 0, r.stdout[-500:]
        # 服务活着(隔离目录)
        with urllib.request.urlopen(f"{BASE}/api/health", timeout=5) as resp:
            assert json.loads(resp.read())["status"] == "ok"
        # 生产数据未被触碰
        after = prod.read_bytes() if prod.exists() else b""
        assert before == after, "生产 default.json 被 E2E 修改了!"
        # 隔离目录里产生了自己的数据
        assert iso.exists() and any(iso.iterdir()), "隔离目录应有 E2E 自己的数据"

        # 清理(复用修过解码的 kill_port; 本文件曾在这里内联 netstat 解析,
        # 同款 GBK 解码 bug 让清理崩掉、服务残留占了 8127 —— 见
        # docs/bug-reflection-2026-10-13.md)
        kill_port(PORT)
