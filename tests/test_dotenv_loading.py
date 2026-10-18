"""`.env` 加载回归测试 —— 文档与代码行为脱节的锁

为什么需要
---------
2026-10-10 TD-10 死信开关上线时, `.env.example` 与 docs/OPERATIONS.md §5
都教用户"把 STOCK_PING_URL 写进 .env", 但**没有任何代码把 .env 加载进
os.environ**:

  - 服务内路径(STOCK_API_TOKEN / STOCK_NOTIFY_*)走 pydantic-settings,
    它有 env_file=".env", 一直是对的
  - 进程外脚本 scripts/paper_daily_fallback.py 直接 os.environ.get(...),
    .env 对它**完全不可见**

后果: 用户按文档配了死信开关, 兜底脚本照样打印"未配置"——
静默失效, 且没有任何测试会发现(兜底 HTTP 行为全是 mock 的,
没人测过"配置从哪来")。

本文件锁住三件事:
  1. 兜底脚本加载 .env(进程外入口)
  2. 加载优先级: 已存在的真实环境变量**不被** .env 覆盖
     (调度器/Docker 注入的环境变量必须优先 —— 否则测试环境会被
     仓库里的 .env 污染, CI 也会跟着挂)
  3. 健壮性: .env 不存在 / 空 / 格式损坏都不崩

注: 这里是**离线**测试 —— 不 ping 真实 URL, 不起服务。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TZ = ZoneInfo("Asia/Shanghai")


@pytest.fixture(scope="module")
def fb():
    """加载 scripts/paper_daily_fallback.py(导入无副作用, 不起服务)"""
    scripts_dir = str(REPO_ROOT / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    path = REPO_ROOT / "scripts" / "paper_daily_fallback.py"
    assert path.is_file(), f"缺少兜底脚本 {path}"
    spec = importlib.util.spec_from_file_location("_paper_daily_fallback", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def clean_env(monkeypatch):
    """清掉可能被本机 .env/环境注入的相关变量, 保证测试不依赖宿主环境"""
    for key in ("STOCK_PING_URL", "FALLBACK_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def _write_env(path: Path, *lines: str) -> None:
    """写 .env —— **必须 newline=""**: 本仓有 LF 行尾锁(红线 5)"""
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("\n".join(lines) + "\n")


class TestDotenvLoading:
    """兜底脚本必须把 .env 加载进 os.environ(TD-10 静默失效的修复)"""

    def test_dotenv_load_function_exists(self, fb):
        """必须有一个可调用的加载入口 —— 这是回归的核心"""
        assert hasattr(fb, "load_dotenv_files"), (
            "兜底脚本缺少 load_dotenv_files() —— .env 不会被加载, "
            "按文档配的 STOCK_PING_URL 不生效(正是 2026-10-10 的静默 bug)"
        )

    def test_ping_url_from_dotenv_file(self, fb, clean_env, tmp_path):
        """正常路径: .env 里有 STOCK_PING_URL -> main() 能读到并 ping"""
        env_file = tmp_path / ".env"
        _write_env(env_file, "STOCK_PING_URL=https://hc-ping.com/from-file")
        clean_env.setattr(fb, "DOTENV_PATH", env_file)

        loaded = fb.load_dotenv_files()
        assert os.environ.get("STOCK_PING_URL") == "https://hc-ping.com/from-file", (
            f"load_dotenv_files() 返回 {loaded!r}, 但环境变量没读进去"
        )
        # 清理: 这个变量只属于本测试, 不能泄漏给后续用例
        os.environ.pop("STOCK_PING_URL", None)

    def test_real_env_var_wins_over_dotenv(self, fb, clean_env, tmp_path):
        """优先级: 真实环境变量**不**被 .env 覆盖

        调度器/Docker 注入的变量必须优先于文件里的值 —— 否则测试环境
        会被仓库自带的 .env 污染(CI 也可能因此挂)。
        """
        env_file = tmp_path / ".env"
        _write_env(env_file, "STOCK_PING_URL=https://hc-ping.com/from-file")
        clean_env.setattr(fb, "DOTENV_PATH", env_file)
        clean_env.setenv("STOCK_PING_URL", "https://hc-ping.com/from-real-env")

        fb.load_dotenv_files()

        assert os.environ["STOCK_PING_URL"] == "https://hc-ping.com/from-real-env", (
            ".env 覆盖了真实环境变量 —— 调度器注入的配置应该优先"
        )
        os.environ.pop("STOCK_PING_URL", None)

    def test_missing_dotenv_file_is_noop(self, fb, clean_env, tmp_path):
        """边界: .env 不存在 -> 静默跳过(首次部署/容器场景没有这个文件)"""
        clean_env.setattr(fb, "DOTENV_PATH", tmp_path / "nonexistent.env")
        fb.load_dotenv_files()
        assert "STOCK_PING_URL" not in os.environ

    def test_empty_and_commented_dotenv(self, fb, clean_env, tmp_path):
        """边界: 空文件/全注释 -> 不产生变量, 也不崩"""
        env_file = tmp_path / ".env"
        _write_env(env_file, "", "# just a comment", "   ", "")
        clean_env.setattr(fb, "DOTENV_PATH", env_file)
        fb.load_dotenv_files()
        assert "STOCK_PING_URL" not in os.environ

    def test_garbage_dotenv_does_not_crash(self, fb, clean_env, tmp_path):
        """异常: 格式损坏的 .env 不能让兜底脚本崩掉

        兜底由外部调度器无人值守调用 —— 配置坏了也得把当天的账补上,
        退出码不能因为 .env 解析失败而变坏(misconfiguration ≠ 不处理)。
        """
        env_file = tmp_path / ".env"
        _write_env(env_file, "这不是合法的行", "=没有键", "STOCK_PING_URL")
        clean_env.setattr(fb, "DOTENV_PATH", env_file)
        fb.load_dotenv_files()  # 不抛异常即通过

    def test_dotenv_loaded_before_main_reads_env(self, fb, clean_env, tmp_path):
        """接线: main() 开头必须加载 .env —— 否则函数再对也没用

        为什么锁"开头"而不是"读取之前": 只断言 load < read 时, 把加载
        挪到 ping_url 之后一行仍满足(变异实测该假锁 1/1 存活)。
        这里钉死: main() 的第一个可执行语句就是加载(注释除外)。
        """
        src = (REPO_ROOT / "scripts" / "paper_daily_fallback.py").read_text(encoding="utf-8")
        main_body = src[src.index("def main() -> int:") :]
        # 去掉 docstring 与注释, 只看语句顺序
        statements = [
            line.strip()
            for line in main_body.splitlines()
            if line.strip()
            and not line.strip().startswith("#")
            and not line.strip().startswith('"""')
        ]
        # 第 0 条是 def main() 签名; 第 1 条必须是 global; 第 2 条必须加载 .env
        assert statements[1].startswith("global "), f"main() 开头异常: {statements[1]!r}"
        assert statements[2] == "load_dotenv_files()", (
            "main() 的第三个语句不是 load_dotenv_files() —— "
            f"实际是 {statements[2]!r}; 加载若晚于任何环境变量读取, "
            "配在 .env 里的 STOCK_PING_URL 就赶不上(变异实测的假锁点)"
        )


class TestDotenvWiringInMain:
    """端到端(mock HTTP): .env 配了 -> 真的会 ping 死信开关"""

    @staticmethod
    def _run_main(monkeypatch, fb, *, env_file: Path):
        calls = []
        sched = {
            "running": True,
            "last_run_at": "",
            "last_status": "",
        }
        monkeypatch.setattr(fb, "server_alive", lambda: True)
        monkeypatch.setattr(
            fb,
            "http_json",
            lambda path, method="GET", payload=None, timeout=30: (
                sched if "schedule" in path and method == "GET" else {"status": "skipped"}
            ),
        )
        monkeypatch.setattr(fb, "ping_quiet", lambda url, ok=True: calls.append((url, ok)))
        monkeypatch.setattr(fb, "DOTENV_PATH", env_file)
        monkeypatch.delenv("STOCK_PING_URL", raising=False)
        return fb.main(), calls

    def test_ping_url_in_dotenv_triggers_ping(self, monkeypatch, fb, tmp_path):
        """完整链路: .env 里配了 URL -> main() 真的去 ping(TD-10 的初心)"""
        env_file = tmp_path / ".env"
        _write_env(env_file, "STOCK_PING_URL=https://hc-ping.com/e2e")
        code, calls = self._run_main(monkeypatch, fb, env_file=env_file)
        assert code == 0
        assert calls == [("https://hc-ping.com/e2e", True)]
        os.environ.pop("STOCK_PING_URL", None)

    def test_no_dotenv_no_ping_no_crash(self, monkeypatch, fb, tmp_path):
        """没有 .env: 不 ping、退出码不受影响(兜底本身最重要)"""
        code, calls = self._run_main(monkeypatch, fb, env_file=tmp_path / "absent.env")
        assert code == 0
        assert calls == []

    def test_fallback_base_url_from_dotenv(self, monkeypatch, fb, tmp_path):
        """.env 里的 FALLBACK_BASE_URL 也要生效(容器部署直连 app 的配法)

        模块级 BASE 在 import 时求值, 早于 .env 加载 —— main() 必须重算,
        否则 .env 里配了也白配(与 STOCK_PING_URL 同源的第二个静默点)。
        """
        env_file = tmp_path / ".env"
        _write_env(env_file, "FALLBACK_BASE_URL=http://app:8000")
        seen = []

        def fake_http(path, method="GET", payload=None, timeout=30):
            seen.append(path)
            return {"status": "ok", "running": True, "last_run_at": "2099-01-01T00:00:00"}

        monkeypatch.setattr(fb, "server_alive", lambda: True)
        monkeypatch.setattr(fb, "http_json", fake_http)
        monkeypatch.setattr(fb, "ping_quiet", lambda url, ok=True: None)
        monkeypatch.setattr(fb, "DOTENV_PATH", env_file)
        monkeypatch.delenv("FALLBACK_BASE_URL", raising=False)

        assert fb.main() == 0
        assert fb.BASE == "http://app:8000", (
            ".env 里的 FALLBACK_BASE_URL 没生效 —— 容器部署的兜底会连错地址"
        )
        os.environ.pop("FALLBACK_BASE_URL", None)
