"""质量门禁自身的回归保护

背景（2026-10-10 体检发现）
--------------------------
`pyproject.toml` 认真配了 `[tool.mypy]`（python_version / warn_return_any /
warn_unused_configs），CI 也 `pip install ruff mypy` —— 然后**只跑 ruff**。

于是 mypy 装了、配了、**从不执行**，存量 86 个错误无人可见。

这是本项目 15 个缺陷的共同形态（「看着有门禁，其实没有」）在**流程**上的翻版，
而这类的代码修复毫无意义：只要有人删掉 CI 里那一行，门禁就又消失了。
所以这里锁的是**「门禁存在且真的在跑」这件事本身**。

注意本文件**不**运行 mypy（那要 20-30 秒 × 4 个 CI job，且和 CI 的 mypy 步骤重复）。
分工是：
  - 本文件（静态、瞬时）→ 保证「门禁被接上了」
  - CI 的 mypy 步骤（动态）  → 保证「门禁能通过」
"""

from __future__ import annotations

import pathlib
import re

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PYPROJECT = REPO_ROOT / "pyproject.toml"

# 匹配"真的在调用 mypy"（而不是 pip install mypy / 注释里提到它）
MYPY_INVOCATION = re.compile(r"(?m)^\s*(?:python\s+-m\s+)?mypy\b")


def _ci_run_commands() -> list[str]:
    """CI 里所有 run 步骤的命令文本"""
    data = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))
    return [
        step["run"]
        for job in data.get("jobs", {}).values()
        for step in job.get("steps", [])
        if isinstance(step, dict) and "run" in step
    ]


def _mypy_commands() -> list[str]:
    return [c for c in _ci_run_commands() if MYPY_INVOCATION.search(c)]


def _pyproject_section(name: str) -> str:
    """取 pyproject 里某个 [section] 的正文（到下一个 [ 开头的行为止）"""
    text = PYPROJECT.read_text(encoding="utf-8")
    match = re.search(rf"(?ms)^\[{re.escape(name)}\]\s*$(.*?)(?=^\[|\Z)", text)
    return match.group(1) if match else ""


def _effective_lines(section_body: str) -> str:
    """只留真正生效的键值行，去掉整行注释

    必要：本项目的配置注释里**会引用** `ignore_missing_imports` 这个键名来解释
    为什么不用它。不剥注释的话，这条锁会因为"注释里提了一嘴"而误报
    （写完这条锁第一次跑就撞上了）。
    """
    lines = [line for line in section_body.splitlines() if not line.lstrip().startswith("#")]
    return "\n".join(lines)


class TestMypyGateIsWired:
    """mypy 必须被 CI **执行**，而不只是被安装"""

    def test_ci_actually_invokes_mypy(self):
        """CI 必须有真的在跑 mypy 的 run 步骤

        变异验证方式：删掉 ci.yml 里 Lint job 的 mypy 步骤，本测试必红。
        """
        mypy_cmds = _mypy_commands()
        assert mypy_cmds, (
            "CI 没有任何执行 mypy 的 run 步骤 —— 就是「装了、配了、从不跑」"
            "那个假门禁（pyproject 的 [tool.mypy] 形同虚设）"
        )

    def test_mypy_step_targets_real_source(self):
        """mypy 必须检查真实源码目录 —— 否则「跑过了」但什么都没查"""
        mypy_cmds = _mypy_commands()
        assert any("src/" in cmd for cmd in mypy_cmds), (
            f"CI 的 mypy 调用没有指向 src/，可能查了个空目录：{mypy_cmds}"
        )

    def test_mypy_step_fails_the_build(self):
        """mypy 出错必须让 CI 失败，不能 `|| true` 兜住

        这与本项目的既有教训一致：静默降级比崩溃更难发现。
        """
        for cmd in _mypy_commands():
            assert "|| true" not in cmd, f"mypy 步骤被 `|| true` 吞掉了失败：{cmd}"
            assert "||true" not in cmd, f"mypy 步骤被 `|| true` 吞掉了失败：{cmd}"

    def test_ci_installs_mypy(self):
        """mypy 得先装上才能跑"""
        text = CI_YML.read_text(encoding="utf-8")
        assert re.search(r"pip install[^\n]*\bmypy\b", text), "CI 没有安装 mypy"


class TestMypyConfigIsHonest:
    """配置不能是摆设，也不能靠全局忽略把问题扫到地毯下"""

    def test_tool_mypy_section_exists(self):
        assert _pyproject_section("tool.mypy").strip(), "pyproject 缺少 [tool.mypy] 配置"

    def test_untyped_deps_are_exempted_explicitly(self):
        """第三方无 stub 的库要在 overrides 里**点名**豁免

        依赖库里 akshare / baostock / plotly / gymnasium / stable_baselines3 /
        pandas_ta / apscheduler 都没有 py.typed，不豁免就是 40+ 个假错误。
        """
        text = PYPROJECT.read_text(encoding="utf-8")
        overrides = re.findall(r"(?ms)^\[\[tool\.mypy\.overrides\]\](.*?)(?=^\[|\Z)", text)
        assert overrides, "缺少 [[tool.mypy.overrides]]：无 stub 的依赖会产生大量假错误"
        joined = "\n".join(overrides)
        for module in ("akshare", "baostock", "plotly", "gymnasium", "pandas_ta"):
            assert module in joined, f"未点名豁免无 stub 的依赖: {module}"

    def test_no_global_ignore_missing_imports(self):
        """**不得**用全局 ignore_missing_imports 一刀切

        全局忽略会让「新引入一个无类型依赖」这件事彻底不可见；
        点名豁免则保证新依赖冒出来时 mypy 会报错、逼人做一次决定。
        """
        effective = _effective_lines(_pyproject_section("tool.mypy"))
        assert not re.search(r"(?m)^\s*ignore_missing_imports\s*=", effective), (
            "[tool.mypy] 里出现了全局 ignore_missing_imports —— "
            "请改为在 [[tool.mypy.overrides]] 里点名豁免具体依赖"
        )


class TestGateConfigMatchesDocs:
    """文档声称的门禁必须与 CI 实际一致（本项目的老毛病）"""

    def test_readme_claims_type_check_matches_ci(self):
        """README 的 CI 小节声称跑什么，CI 就得真跑什么"""
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        claims_type_check = "类型检查" in readme or "mypy" in readme
        if not claims_type_check:
            pytest.skip("README 未声称做类型检查，无需比对")
        assert _mypy_commands(), "README 声称做类型检查，但 CI 里没有执行 mypy 的步骤"
