"""Bug 回归保护测试 (QM-5 ④)

本文件针对 4 个已确认并修复的 bug 建立回归保护。每个测试对应一个真实逃逸漏洞，
防止同类问题再次出现。

背景：这4个bug的共同特征是「测试全绿(396 passed)但缺陷真实存在」——
测试只验证了「函数返回了东西」，没有验证「返回值语义正确」。

Bug 引入点:
  - Bug 1/2: c7feba9 (Phase 2 complete) / 59d7907 (add TradingPipeline)
  - Bug 3:   3c9a3c6 (commit 标题写着 "fix demo.py API names"，实际引入了错误 key)
  - Bug 4:   3bc855a (bump version to 0.5.0，只改了 __init__.py)
"""

import pathlib
import re
from functools import lru_cache

import pandas as pd
import pytest

from stock_model.pipeline.config import PipelineConfig
from stock_model.pipeline.models import PipelineStatus
from stock_model.pipeline.trading_pipeline import TradingPipeline
from stock_model.risk.manager import RiskManager
from stock_model.risk.models import Position
from stock_model.strategy.base import ActionType, BaseStrategy, StrategyResult

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


@lru_cache(maxsize=1)
def _src_filename_index() -> frozenset[str]:
    """src/ 下所有 .py 文件名的索引(只构建一次)

    rglob 在 NFS 工作区上很慢, 逐用例重建曾使单用例耗时 20+ 秒。
    """
    return frozenset(p.name for p in (REPO_ROOT / "src").rglob("*.py"))


def _read_pyproject_version() -> str:
    """从 pyproject.toml 提取 [project] 段声明的版本号

    刻意不用 tomllib: 它是 Python 3.11+ 才有的标准库，而本项目
    requires-python >= 3.10，CI 跑 3.10 矩阵。改用正则保持 3.10 兼容。
    """
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    # 定位 [project] 段，避免误匹配其他段里的 version
    project_section = text.split("[project]", 1)[1]
    match = re.search(r'^version\s*=\s*"([^"]+)"', project_section, re.MULTILINE)
    assert match, "未能在 pyproject.toml 的 [project] 段找到 version 声明"
    return match.group(1)


class _BuyStrategy(BaseStrategy):
    """始终给出买入信号的测试策略"""

    name = "buy_always"

    def analyze(self, symbol, df):
        return StrategyResult(
            symbol=symbol,
            action=ActionType.BUY,
            confidence=0.9,
            target_price=float(df["close"].iloc[-1]),
        )

    def evaluate(self, symbol, df):
        return {"win_rate": 0.6}


def _make_df(n=60):
    """构造 n 日行情数据"""
    return pd.DataFrame(
        {
            "open": [10.0 + i * 0.1 for i in range(n)],
            "high": [10.5 + i * 0.1 for i in range(n)],
            "low": [9.5 + i * 0.1 for i in range(n)],
            "close": [10.0 + i * 0.1 for i in range(n)],
            "volume": [100000.0 + i for i in range(n)],
        },
        index=pd.date_range("2024-01-01", periods=n, freq="B"),
    )


# ========================================================================
# Bug 2 回归保护：流水线风控检查是死代码
# ========================================================================
# 逃逸原因：59d7907 引入 TradingPipeline 时，_check_risk 构造 Position
#           写死 shares=0，而 RiskManager.check_position_risk 首行
#           `if position.shares <= 0: return alerts` 直接短路。
#           同 commit 新增的测试里 grep "risk|BLOCKED" 命中数为 0。


class TestBug2PipelineRiskCheck:
    """流水线风控检查必须真实可触发 BLOCKED"""

    def test_check_risk_returns_alerts_not_empty(self):
        """_check_risk 不能恒返回空列表

        构造「现价已跌破止损价」的场景: 策略建议以 cost 买入,
        止损线设在 cost*0.8, 而最新价已跌到 cost*0.7 → 应触发 CRITICAL 止损。
        """
        pipeline = TradingPipeline(PipelineConfig(enable_notify=False, min_data_rows=10))
        cost = 10.0
        # 止损线 8.0, 现价已跌到 7.0 → 跌破止损
        sr = StrategyResult(
            symbol="000001",
            action=ActionType.SELL,
            confidence=0.9,
            target_price=cost,
            stop_loss=cost * 0.8,
        )
        alerts = pipeline._check_risk("000001", sr, current_price=cost * 0.7)  # 内部方法白盒测试

        assert len(alerts) > 0, (
            "现价跌破止损价时 _check_risk 应当返回止损警报 —— "
            "返回空说明 shares 占位 0 导致 RiskManager 短路（历史 bug）"
        )
        assert any(a.level.value == "critical" for a in alerts)

    def test_risk_manager_short_circuit_is_documented(self):
        """RiskManager 的 shares<=0 短路是有意设计，测试锁定该行为

        这是逃逸漏洞的根源。若未来有人误以为流水线能传 shares=0 拿到警报，
        这个测试会提醒他看 _check_risk 的实现。
        """
        rm = RiskManager(max_drawdown_limit=0.15)
        zero_pos = Position(symbol="000001", shares=0, cost_price=10.0, current_price=10.0)
        assert rm.check_position_risk(zero_pos) == []

    def test_run_once_can_reach_blocked_status(self, monkeypatch):
        """端到端: 注入 CRITICAL 警报后，run_once 必须返回 BLOCKED

        这是最关键的一条 —— 证明 BLOCKED 状态在生产路径上可达。
        """
        pipeline = TradingPipeline(
            PipelineConfig(enable_notify=False, min_data_rows=10, enable_risk_check=True)
        )
        pipeline.add_strategy(_BuyStrategy())

        df = _make_df(60)
        monkeypatch.setattr(pipeline, "_fetch_data", lambda symbol: df)
        monkeypatch.setattr(pipeline, "_check_quality", lambda symbol, d: 1.0)

        # 强制风控返回一条 CRITICAL 警报
        from stock_model.risk.models import RiskAlert, RiskLevel, RiskType

        critical = RiskAlert(
            level=RiskLevel.CRITICAL,
            type=RiskType.STOP_LOSS,
            message="强制注入的止损警报",
            action="立即卖出",
            symbol="000001",
        )
        monkeypatch.setattr(pipeline, "_check_risk", lambda symbol, sr, price=0.0: [critical])

        result = pipeline.run_once("000001")
        assert result.status == PipelineStatus.BLOCKED
        assert "风控拦截" in result.reason

    def test_run_once_blocks_on_real_stop_loss_violation(self, monkeypatch):
        """不注入任何 mock: 真实风控必须能拦下荒谬的止损参数

        策略给出「止损价 = 2倍现价」——逻辑上不可能成交的订单。
        若风控真正执行，Position(current_price) <= stop_loss 成立，
        应产生 CRITICAL 警报并让 run_once 返回 BLOCKED。

        这一条比注入式测试更强：它验证生产代码路径本身，不依赖测试替身。
        """

        class _BadStopStrategy(_BuyStrategy):
            """止损价高于买入价 —— 逻辑上不可能成交，必须被拦"""

            def analyze(self, symbol, df):
                current = float(df["close"].iloc[-1])
                return StrategyResult(
                    symbol=symbol,
                    action=ActionType.BUY,
                    confidence=0.9,
                    target_price=current,
                    stop_loss=current * 2,  # 止损价 = 2倍现价
                )

        pipeline = TradingPipeline(
            PipelineConfig(enable_notify=False, min_data_rows=10, enable_risk_check=True)
        )
        pipeline.add_strategy(_BadStopStrategy())

        df = _make_df(60)
        monkeypatch.setattr(pipeline, "_fetch_data", lambda symbol: df)
        monkeypatch.setattr(pipeline, "_check_quality", lambda symbol, d: 1.0)

        result = pipeline.run_once("000001")
        assert result.status == PipelineStatus.BLOCKED, (
            f"止损价高于现价时应被风控拦截，实际状态={result.status}"
        )
        assert result.risk_alerts

    def test_pipeline_position_has_nonzero_shares(self):
        """_check_risk 构造的 Position 必须有非零 shares

        直接锁定回归点：如果有人把 shares 改回 0，这条测试会红。
        """
        import inspect

        from stock_model.pipeline import trading_pipeline as tp_mod

        src = inspect.getsource(tp_mod.TradingPipeline._check_risk)  # 内部方法白盒测试
        assert "shares=0" not in src, (
            "_check_risk 不能再构造 shares=0 的 Position —— "
            "这会让 RiskManager 短路返回空列表，使风控形同虚设"
        )


# ========================================================================
# Bug 3 回归保护：demo.py 读取不存在的 metrics key
# ========================================================================
# 逃逸原因：3c9a3c6 的 commit 标题是 "fix demo.py API names"，
#           但它写入的是 sharpe_ratio / trade_count，
#           而 BacktestEngine._calculate_metrics 实际写的是 sharpe / total_trades。
#           因为用了 .get(key, 0) 兜底，不报错、静默输出 0。


class TestBug3DemoMetricsKeys:
    """demo.py 的 metrics key 必须与 BacktestEngine 实际产出一致"""

    def test_demo_reads_real_metric_keys(self):
        """demo.py 读取的 key 必须真实存在于 BacktestResult.metrics"""
        from stock_model.strategy.engine import BacktestEngine
        from stock_model.strategy.manual import ManualStrategy

        result = BacktestEngine(initial_cash=100000).run(ManualStrategy(), _make_df(200), "000001")
        actual_keys = set(result.metrics)

        demo_src = (REPO_ROOT / "examples" / "demo.py").read_text(encoding="utf-8")
        # 提取 demo.py 里所有 metrics.get('xxx') 的 key
        used_keys = set(re.findall(r"metrics\.get\(['\"]([\w_]+)['\"]", demo_src))

        assert used_keys, "demo.py 应当仍在读取 metrics"
        missing = used_keys - actual_keys
        assert not missing, (
            f"demo.py 读取了不存在的 metrics key: {sorted(missing)}。\n"
            f"BacktestEngine 实际产出: {sorted(actual_keys)}\n"
            f"这类错误不会抛异常，只会让 demo 静默打印 0。"
        )

    def test_no_legacy_metric_keys_in_demo(self):
        """锁定具体的历史错误 key 不再出现"""
        demo_src = (REPO_ROOT / "examples" / "demo.py").read_text(encoding="utf-8")
        for bad_key in ("sharpe_ratio", "trade_count"):
            assert bad_key not in demo_src, (
                f"demo.py 不应再使用 '{bad_key}'（正确 key: "
                f"{'sharpe' if bad_key == 'sharpe_ratio' else 'total_trades'}）"
            )


# ========================================================================
# Bug 2b 回归保护：策略聚合丢弃价格字段
# ========================================================================
# 逃逸原因：StrategyEngine.aggregate_signal 返回全新的 StrategyResult，
#           只复制 symbol/action/confidence，
#           target_price/stop_loss/position_pct 全部丢失。
#           结果是即使修好 shares=0，流水线拿到的仍是空价格字段。


class TestBug2bAggregateSignalPreservesPrices:
    """aggregate_signal 必须把价格字段传递给下游"""

    def _engine(self):
        from stock_model.strategy.engine import StrategyEngine

        return StrategyEngine()

    def test_single_strategy_prices_survive(self):
        engine = self._engine()
        result = engine.aggregate_signal(
            {
                "manual": StrategyResult(
                    symbol="000001",
                    action=ActionType.BUY,
                    confidence=0.8,
                    target_price=12.5,
                    stop_loss=9.0,
                    position_pct=30.0,
                )
            }
        )
        assert result.target_price == 12.5
        assert result.stop_loss == 9.0
        assert result.position_pct == 30.0

    def test_prices_come_from_highest_confidence_strategy(self):
        """价格必须与动作同源 —— 否则风控会用 A 的止损去管 B 的信号"""
        engine = self._engine()
        result = engine.aggregate_signal(
            {
                "low_conf": StrategyResult(
                    "000001", ActionType.BUY, 0.2, "w", target_price=1.0, stop_loss=0.9
                ),
                "high_conf": StrategyResult(
                    "000001", ActionType.BUY, 0.9, "s", target_price=99.0, stop_loss=88.0
                ),
            }
        )
        assert result.target_price == 99.0
        assert result.stop_loss == 88.0
        assert result.metadata["price_source_strategy"] == "high_conf"

    def test_empty_results_still_safe(self):
        engine = self._engine()
        result = engine.aggregate_signal({})
        assert result.action == ActionType.HOLD


# ========================================================================
# Bug 1 回归保护：README 引用不存在的模块
# ========================================================================
# 逃逸原因：c7feba9 声称实现 Phase 2 含 momentum.py，但该文件从未落地；
#           README 照着不存在的实现写示例代码，CI 无任何校验。


class TestBug1ReadmeImportsExist:
    """README 中出现的 stock_model import 必须真实可导入"""

    def test_readme_stock_model_imports_are_importable(self):
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        # 匹配 `from stock_model... import X` 和 `import stock_model...`
        patterns = re.findall(r"from\s+(stock_model[\w.]*)\s+import\s+([\w, ]+)", readme)
        assert patterns, "README 应包含可执行的 import 示例"

        import importlib

        for module_name, names in patterns:
            module = importlib.import_module(module_name)
            for raw_name in names.split(","):
                attr = raw_name.strip()
                if not attr or attr == "*":
                    continue
                assert hasattr(module, attr), (
                    f"README 示例 `from {module_name} import {attr}` 无法执行 —— "
                    f"该名称在模块中不存在。这会直接让新用户 ImportError。"
                )

    def test_momentum_not_referenced_in_readme(self):
        """momentum.py 从未实现，README 不应再引用"""
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        assert "MomentumStrategy" not in readme
        assert "strategy.momentum" not in readme


# ========================================================================
# Bug 4 回归保护：版本号三处不一致
# ========================================================================
# 逃逸原因：3bc855a "bump version to 0.5.0" 只改了 __init__.py 的
#           __version__，没同步 pyproject.toml。发布流程读 pyproject，
#           所以打出去的包永远停在 0.1.0。


class TestBug4VersionConsistency:
    """pyproject.toml 与 __init__.py 的版本号必须一致"""

    def test_pyproject_version_matches_init(self):
        import stock_model

        declared = _read_pyproject_version()
        actual = stock_model.__version__

        assert declared == actual, (
            f"版本号不一致: pyproject.toml={declared}, __init__.py={actual}。\n"
            f"发布流程读取 pyproject.toml，会打出错误版本号。"
        )

    def test_version_is_semver(self):
        import stock_model

        assert re.fullmatch(r"\d+\.\d+\.\d+", stock_model.__version__), (
            f"版本号 {stock_model.__version__} 不是合法的 semver"
        )


# ========================================================================
# 通用防回归：README 代码块语法 + 关键模块可导入
# ========================================================================


class TestGeneralIntegrity:
    """通用完整性检查，防止同类逃逸再次发生"""

    def test_all_public_modules_importable(self):
        """核心模块必须可导入（不依赖可选重依赖）"""
        import importlib

        core_modules = [
            "stock_model",
            "stock_model.analysis.signals",
            "stock_model.analysis.technical",
            "stock_model.data.fetcher",
            "stock_model.pipeline.trading_pipeline",
            "stock_model.risk.manager",
            "stock_model.strategy.engine",
        ]
        for name in core_modules:
            importlib.import_module(name)

    def test_no_python_syntax_errors_in_repo(self):
        """所有项目源码必须能通过语法解析

        注意: 用 os.walk 提前剪枝而非 ``rglob`` —— rglob 会先生成全部
        路径再逐个过滤, 而 .venv 下有数千个包文件, 在 NFS 工作区上
        仅遍历就要 190 秒(实测), 会拖垮整个测试套件。
        """
        import ast
        import os

        skip_dirs = {".venv", "venv", "__pycache__", ".git", "node_modules"}
        checked = 0
        for root, dirs, files in os.walk(REPO_ROOT):
            # 就地剪枝, 避免进入依赖目录
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            for name in files:
                if not name.endswith(".py"):
                    continue
                py_file = pathlib.Path(root) / name
                ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
                checked += 1

        assert checked > 10, f"只检查到 {checked} 个 .py 文件, 遍历范围可能不对"

    @pytest.mark.parametrize(
        "doc",
        ["README.md", "docs/phase2_architecture.md", "docs/phase3_prd.md"],
    )
    def test_docs_reference_existing_source_files(self, doc):
        """文档中提到的 src/ 路径必须真实存在"""
        text = (REPO_ROOT / doc).read_text(encoding="utf-8")
        # 匹配 `xxx.py` 形式的源码引用
        refs = set(re.findall(r"[\w/]+\.py\b", text))
        if not refs:
            return

        # 源码文件名索引只构建一次: rglob 在 NFS 工作区上很慢,
        # 每个用例重建索引曾使单个用例耗时 20+ 秒(实测)。
        idx = _src_filename_index()
        missing = []
        for ref in refs:
            if "/" in ref and not (REPO_ROOT / ref).exists():
                # 可能是 strategy/xxx.py 这种简写，尝试按文件名在 src 下找
                if ref.split("/")[-1] not in idx:
                    missing.append(ref)
        assert not missing, f"{doc} 引用了不存在的源码文件: {sorted(missing)}"
