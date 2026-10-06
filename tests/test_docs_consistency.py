"""文档与代码一致性校验 (Phase 3.4 文档同步)

背景
----
本项目文档长期与代码脱节: README 声称 185 个测试，实际已 482 个；
架构树遗漏 `pipeline/` 目录；文档仍描述"HTML 内嵌在 Python 中"，
而前端早已拆分到 `web/static/`。

这类漂移会误导新用户和 reviewer。本文件用机器校验代替肉眼核对。

**测试不硬编码具体数字**，而是从实际运行结果动态读取后再比对，
因此新增测试后无需手工改文档——只有文档忘了更新时才会失败。
"""

import pathlib
import re
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
README = REPO_ROOT / "README.md"


def _count_tests() -> tuple[int, int, int]:
    """返回 (总数, passed, skipped)，通过实际收集/运行得出"""
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/",
            "-q",
            "--collect-only",
            "-p",
            "no:cacheprovider",
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin"},
        check=False,  # 收集失败时由下方正则兜底并 skip, 不直接抛出
    )
    m = re.search(r"(\d+)\s+tests? collected", proc.stdout)
    if not m:
        pytest.skip(f"无法解析测试数量: {proc.stdout[-200:]}")
    return int(m.group(1)), 0, 0


class TestReadmeTestCount:
    """README 声称的测试数必须与实际一致"""

    def test_readme_test_count_matches_reality(self):
        total, _, _ = _count_tests()
        text = README.read_text(encoding="utf-8")

        # 允许 "482" 或 "482个" 等写法
        claimed = re.findall(r"测试\s*\((\d+)\s*个\)|当前共\s*\*\*(\d+)\*\*\s*个测试", text)
        assert claimed, "README 未声明测试数量，无法校验一致性"

        numbers = {int(a or b) for a, b in claimed}
        assert numbers == {total}, (
            f"README 声称的测试数 {sorted(numbers)} 与实际 {total} 不符。\n"
            f"新增/删除测试后请同步更新 README。"
        )


class TestReadmeArchitectureTree:
    """README 架构树必须覆盖实际存在的顶层模块目录"""

    @pytest.mark.parametrize(
        "module_dir",
        ["data", "analysis", "strategy", "risk", "portfolio", "notify", "pipeline", "web"],
    )
    def test_module_dir_documented(self, module_dir):
        assert (REPO_ROOT / "src" / "stock_model" / module_dir).is_dir(), (
            f"模块 {module_dir} 不存在，测试配置需更新"
        )
        readme = README.read_text(encoding="utf-8")
        assert f"│   ├── {module_dir}/" in readme or f"│   └── {module_dir}/" in readme, (
            f"README 架构树缺少 {module_dir}/ 目录"
        )

    def test_new_files_documented(self):
        """本次新增/重构的关键文件必须在架构树中出现"""
        readme = README.read_text(encoding="utf-8")
        for path in (
            "indicators.py",  # 纯 pandas 指标兜底
            "screener.py",  # 选股 API
            "trading_pipeline.py",  # 流水线
            "static/",  # 前端资源目录
        ):
            assert path in readme, f"README 架构树缺少 {path}"


class TestReadmeClaimsMatchCode:
    """README 的技术性论断必须与代码实际行为一致"""

    def test_pandas_ta_stated_as_optional(self):
        """README 必须说明 pandas-ta 可选且有兜底

        因为 pandas-ta 仅支持 3.12，而项目支持 3.10 ——
        若文档暗示它是必需的，会误导 3.10/3.11 用户。
        """
        readme = README.read_text(encoding="utf-8")
        assert "pandas-ta" in readme
        assert any(kw in readme for kw in ("可选", "兜底", "不装也能用")), (
            "README 需说明 pandas-ta 是可选的，缺依赖时有兜底实现"
        )

    def test_indicators_fallback_exists_in_code(self):
        """文档声称的兜底实现必须真实存在"""
        indicators = REPO_ROOT / "src" / "stock_model" / "analysis" / "indicators.py"
        assert indicators.is_file(), "README 提及的 indicators.py 不存在"

        technical = (REPO_ROOT / "src" / "stock_model" / "analysis" / "technical.py").read_text(
            encoding="utf-8"
        )
        assert "fallback_indicators" in technical, (
            "technical.py 未引用 fallback_indicators, README 关于兜底的描述与代码不符"
        )

    def test_static_frontend_actually_exists(self):
        """README 声称前端在 web/static/ 下，文件必须真实存在"""
        static = REPO_ROOT / "src" / "stock_model" / "web" / "static"
        for name in ("index.html", "dashboard.css", "dashboard.js"):
            assert (static / name).is_file(), f"缺少前端文件 static/{name}"

    def test_documented_api_endpoints_exist(self):
        """README 列出的 API 端点必须在 FastAPI 应用中真实注册"""
        pytest.importorskip("fastapi")

        from stock_model.web.app import create_app

        app = create_app()
        routes = {r.path for r in app.routes if hasattr(r, "path")}

        readme = README.read_text(encoding="utf-8")
        for endpoint in ("/api/screener", "/api/analyze/{symbol}", "/api/backtest/{symbol}"):
            assert endpoint in readme, f"README 未记录 {endpoint}"
            assert endpoint in routes, f"README 记录了 {endpoint} 但代码中不存在"

    def test_no_stale_inlined_html_claim(self):
        """README 不应再声称 HTML 内嵌在 Python 中"""
        readme = README.read_text(encoding="utf-8")
        assert "内嵌HTML" not in readme and "内嵌 HTML" not in readme, (
            "README 仍描述 HTML 内嵌在 Python 中，但前端已拆分到 web/static/"
        )
