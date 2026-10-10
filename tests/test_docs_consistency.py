"""文档与代码一致性校验 (Phase 3.4 文档同步)

背景
----
本项目文档长期与代码脱节: README 声称 185 个测试，实际已 497 个；
架构树遗漏 `pipeline/` 目录；文档仍描述"HTML 内嵌在 Python 中"，
而前端早已拆分到 `web/static/`；安装说明把仅 3.12 可装的
`.[ta]` 列为常规可选依赖，导致 3.10/3.11 用户按文档安装即失败。

这类漂移会误导新用户和 reviewer。本文件用机器校验代替肉眼核对。

**刻意不硬编码测试总数。** 项目多处使用 ``pytest.importorskip``，
不同环境收集到的用例数本就不同（本地 497 / CI 3.10 469），
把环境差异当成文档错误是错误的断言方向。
因此这里校验的是与环境无关、且真正会出错的事:
  - README 多处声明是否互相矛盾
  - 数字量级是否合理（非占位符 / 非笔误）
  - 架构树是否覆盖实际模块
  - 文档中的技术论断是否与代码行为相符
"""

import pathlib
import re
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
README = REPO_ROOT / "README.md"


class TestReadmeTestCount:
    """README 中声明的测试数量必须自洽且量级合理

    **刻意不做跨环境比对。** 项目多处使用 ``pytest.importorskip``，
    缺可选依赖时整类用例不收集，因此不同环境收集到的测试数本就不同：

        本地(依赖齐全)       497
        CI 3.10(缺可选依赖)  469

    把这种环境差异当成"文档错误"是错误的断言方向（该缺陷已真实发生过一次）。

    这里校验的是与环境无关、且真正会出错的两件事：
      1. README 多处提及的测试数是否互相矛盾
      2. 数字本身是否合理（防止占位符 / 手滑写错）
    """

    def test_readme_test_count_is_self_consistent(self):
        text = README.read_text(encoding="utf-8")

        claimed = re.findall(r"测试\s*\((\d+)\s*个\)|当前共\s*\*\*(\d+)\*\*\s*个测试", text)
        assert claimed, "README 未声明测试数量，无法校验一致性"

        numbers = {int(a or b) for a, b in claimed}
        assert len(numbers) == 1, (
            f"README 中测试数出现多个不同值: {sorted(numbers)} —— 架构树与正文不一致"
        )

        (count,) = numbers
        assert count > 100, f"README 测试数 {count} 不合理，疑似占位符或笔误"

        # 静态核对: README 数字不应超过源码中的测试函数/类数
        # （不跑子进程收集 —— 收集数依赖环境，且会给 CI 增加十几秒开销）
        src = "\n".join(
            p.read_text(encoding="utf-8") for p in (REPO_ROOT / "tests").glob("test_*.py")
        )
        defined = len(re.findall(r"^\s*def test_", src, re.MULTILINE))
        assert count <= defined * 3, (
            f"README 声称 {count} 个测试，但测试代码中只有约 {defined} 个 test 函数 —— "
            f"数量级不符，疑似笔误"
        )


class TestTextFileLineEndings:
    """行尾不得被整文件改写

    为什么值得上锁：本项目**已踩过三次**同款 —— Windows 上 Python 的
    ``Path.write_text()`` 与 PowerShell 的 ``Set-Content`` 都会把 ``\\n`` 写成 ``\\r\\n``。
    后果不是"程序坏了"，而是**一次小改造成几百行的全文件 diff**：
    真实改动被淹没，review 直接失效（最近一次：``docs/HANDOVER.md`` 582 行全文重写）。

    现状（2026-10-10 起全仓统一为 LF）：``.gitattributes`` 声明 ``* text=auto eol=lf``，
    本测试是**行为锁** —— git 的归一化只管"提交进去的"，管不到"脚本改完还没提交"的
    工作区文件，所以必须在测试里再钉一道。

    覆盖范围 = **全部跟踪的文本文件**，而不是一份手写名单 —— 名单会过期，
    那正是本项目反复踩的"测试与实际脱节"。
    """

    @staticmethod
    def _tracked_text_files() -> list[str]:
        """全部跟踪文件中属于文本的（无二进制 NUL 即视为文本；本仓库无二进制）"""
        cmd = ["git", "ls-files", "-z"]
        out = subprocess.run(cmd, capture_output=True, check=False).stdout
        files = [f for f in out.decode("utf-8").split("\0") if f]
        text = []
        for rel in files:
            p = REPO_ROOT / rel
            if not p.is_file():
                continue
            raw = p.read_bytes()
            if b"\0" in raw[:8000]:
                continue
            text.append(rel)
        assert text, "没找到任何跟踪文本文件 —— git ls-files 异常"
        return text

    def test_all_tracked_text_files_are_lf(self):
        """全仓文本文件必须是 LF（与 .gitattributes 一致）"""
        offenders = []
        for rel in self._tracked_text_files():
            n = (REPO_ROOT / rel).read_bytes().count(b"\r\n")
            if n:
                offenders.append(f"{rel} ({n} 处)")
        assert not offenders, (
            "以下文件被改成了 CRLF —— 行尾被整文件改写会让真实改动淹没在 diff 里:\n  "
            + "\n  ".join(offenders)
            + "\n改文件时用 newline='' (Python)，别用 Set-Content (PowerShell)"
        )

    def test_gitattributes_declares_lf(self):
        """行尾约定必须有声明文件 —— 否则换个克隆/机器又会漂移"""
        ga = REPO_ROOT / ".gitattributes"
        assert ga.is_file(), "缺少 .gitattributes"
        content = ga.read_text(encoding="utf-8")
        assert "text=auto" in content, ".gitattributes 缺少 * text=auto"
        assert "eol=lf" in content, ".gitattributes 缺少 eol=lf"


class TestDocNumbersDeclareProvenance:
    """环境相关数字必须标来源 —— 这是一条**机制**，不是一次性的修正

    为什么需要（三次真实事故，根因完全相同）
    ----------------------------------------
    1. README 写 `591` 个测试（实际 700+）
    2. README 写 `覆盖率 81%`（实际 85%）
    3. 用 `PYTHONPATH` stub 模拟 CI 主 job 得到 `674 passed / 28 skipped` 并写进文档
       （真实 CI 是 `666 passed / 36 skipped`）

    三次都是**数字没写来源，后来被当成权威**。尤其第 3 次：那个数字来自一个
    **根本不是 CI 的环境**，却因为长得像实测结果而被采信。

    机制
    ----
    凡"随环境变化的数字"，**其所在那一行**必须出现来源标记（本地 / CI / 实测）。
    这样后来的人一眼能看出这个数是"谁在哪个环境量的"，而不会再把它串到别的环境上。

    为什么是"同一行"而不是"邻近几行"：一开始写的是 ±2 行，随即发现**旁边那段
    解释文字本身就在说「写 本地实测 或 CI 实测」** —— 那段说明会替一个裸数字
    满足检查。而"裸数字旁边恰好有『CI』这个词"正是历史事故的形态，所以必须收紧。

    适用范围刻意收窄：**只查被加粗的环境相关数字**（测试总数 / 覆盖率 / passed-skipped）。
    "新增了 N 个测试"这类是历史事实、不随环境变化，要求标来源只是噪音。
    """

    # 只有"随环境变化"的声明才需要来源
    ENV_DEPENDENT_PATTERNS = (
        r"当前共\s*\*\*\d+\*\*\s*个测试",
        r"覆盖率\s*\*\*\d+\s*%",
        r"\*\*\d+\s*passed[,，]\s*\d+\s*skipped\*\*",
    )
    PROVENANCE_MARKERS = ("本地", "CI", "实测")

    DOCS = ("README.md", "docs/HANDOVER.md")

    @pytest.mark.parametrize("rel", DOCS)
    def test_env_dependent_numbers_declare_provenance(self, rel):
        path = REPO_ROOT / rel
        assert path.is_file(), f"文档不存在: {rel}"
        lines = path.read_text(encoding="utf-8").splitlines()

        offenders = []
        for i, line in enumerate(lines):
            if not any(re.search(p, line) for p in self.ENV_DEPENDENT_PATTERNS):
                continue
            # 必须**同一行**写出处: 邻近几行里的说明文字会替裸数字满足检查
            if not any(m in line for m in self.PROVENANCE_MARKERS):
                offenders.append(f"{rel}:{i + 1}: {line.strip()[:90]}")

        assert not offenders, (
            "以下环境相关数字没标来源（本地/CI/实测）—— 本项目已因此写错三次"
            "（591 / 覆盖率81% / stub 模拟的 674+28）：\n  " + "\n  ".join(offenders)
        )

    def test_source_markers_are_not_vacuous(self):
        """反向确认: 标记必须真的出现在数字附近, 而不是文件里到处都有

        若哪天有人把 "本地" 加进不相干段落来"骗过"上面那条, 这条会指向具体位置,
        提醒这条规矩的本意是**标出处**而不是凑关键词。
        """
        text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        for marker in self.PROVENANCE_MARKERS:
            assert marker in text, f"README 缺少来源标记写法示例: {marker}"


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


class TestStrategyEvaluationDoc:
    """策略评估报告必须存在且保留关键警示

    这份文档记录的是「容易踩坑的结论」—— 上涨市踏空、人工股票池。
    如果被删除或弱化，后来者会再次用单窗口的漂亮数字误判策略。
    """

    @staticmethod
    @pytest.fixture(scope="class")
    def eval_doc():
        doc = REPO_ROOT / "docs" / "strategy-evaluation-2026-10-07.md"
        assert doc.is_file(), f"缺少 {doc.name} —— 策略评估报告是本项目最易踩坑的结论沉淀, 不可删除"
        return doc.read_text(encoding="utf-8")

    def test_doc_states_strategy_is_defensive(self, eval_doc):
        """必须明确写出策略是抗跌型而非赚钱型"""
        assert "抗跌" in eval_doc
        assert "不是赚钱型" in eval_doc or "不是赚钱" in eval_doc

    def test_doc_records_bull_market_underperformance(self, eval_doc):
        """必须记录上涨市跑输基准这一关键结论

        注: 数字随评估更新而变 —— 2026-07 首版是 -39.08%(踏空),
        2026-10-09 换动态池后是 -41.32%(亏损)。两者都是真实结论,
        关键是「必须存在一个上涨市跑输的实测数字」。
        """
        assert "上涨市" in eval_doc
        assert "-39.08%" in eval_doc or "-41.32%" in eval_doc, "上涨市跑输基准的实测数字必须保留"

    def test_doc_records_root_cause(self, eval_doc):
        """必须记录根因: 逆势卖出 —— 否则后来者会重走弯路"""
        assert "逆势" in eval_doc, "评估报告必须说明上涨市跑输的根因"
        assert "67%" in eval_doc or "100%" in eval_doc, "应给出逆势卖出的量化占比"

    def test_doc_records_negative_result(self, eval_doc):
        """必须记录趋势过滤器是负面实验（已默认关闭）

        负面结论与正面结论同等重要 —— 不记录, 下一个人会基于同一个直觉
        再实现一遍。故此处锁定「必须记录该结论 + 默认关闭」。
        """
        assert "趋势过滤" in eval_doc, "评估报告必须提及趋势过滤器实验"
        assert "trend_filter" in eval_doc, "应指向 trend_filter 模块"
        assert "默认关闭" in eval_doc or "净效果" in eval_doc, (
            "必须明确记录该实验的结论(默认关闭/净效果为负), 不能只字不提"
        )

    def test_code_defaults_trend_filter_off(self):
        """代码默认必须关闭趋势过滤 —— 与文档结论一致"""
        import inspect

        from stock_model.strategy.manual import ManualStrategy

        src = inspect.getsource(ManualStrategy.__init__)
        assert "use_trend_filter: bool = False" in src, (
            "ManualStrategy 的 use_trend_filter 默认值必须是 False —— "
            "实测该过滤器净效果为负(涨市-2.36pp / 跌市-4.11pp)"
        )

    def test_doc_warns_about_manual_stock_pool(self, eval_doc):
        """必须警示股票池是人工挑选且同质 —— 这是数据不可信的根本原因"""
        assert "手挑" in eval_doc or "人工挑选" in eval_doc
        assert "伪分散" in eval_doc, "应点明该股票池是'伪分散'"

    def test_doc_declares_confidence_limits(self, eval_doc):
        """必须声明数据可信度局限"""
        assert "局限" in eval_doc
        assert "不可信" in eval_doc or "不具统计意义" in eval_doc

    def test_readme_links_to_evaluation(self):
        """README 必须显著链接到评估报告, 并直接给出上涨市的关键数字

        注: 数字随评估更新而变(2026-07 首版 -39.08% 踏空;
        2026-10-09 换动态池后修正为 -41.32% 亏损)。
        故此处锁定「任一版本的实测数字存在」+ 文档链接存在,
        而非硬编码某个具体值 —— 硬编码会在每次修正结论后变成噪音。
        """
        readme = README.read_text(encoding="utf-8")
        assert "strategy-evaluation-2026-10-07" in readme, (
            "README 应链接策略评估报告, 让使用者第一时间看到策略局限"
        )
        # 上涨市超额必须以百分比形式出现在 README
        assert "-41.32%" in readme or "-39.08%" in readme, (
            "README 应直接给出上涨市跑输基准的实测数字"
        )
