"""RL Agent 加载失败静默化 回归保护 (QM-5 ④)

问题
----
``RLTradingAgent.load()`` 此前无论成败都返回 ``None``，失败时只 log 一条
warning。调用方**无法区分成功与失败**——而 ``is_trained`` 属性全项目
无人使用，等于没有任何机制能发现加载失败。

后果: 加载失败的 Agent 会静默降级到规则策略，而
``analyze()`` 照常返回正常的 StrategyResult，
从外部完全看不出"RL 模型其实没加载上"。

这与本系列已修复的多起缺陷同属一类:
**失败被静默吞掉，上层却以为成功。**

另有一起相邻问题: ``tests/test_trading_pipeline.py`` 中
``test_risk_check_exception_returns_empty`` 断言写成

    assert result.status in [EXECUTED, SKIPPED, BLOCKED]

三种状态都算通过 = 什么都没断言，而该文件头注释却宣称覆盖「风控拦截」。
已一并修正为精确断言。
"""

from stock_model.strategy.rl_agent import RLTradingAgent


class TestRLAgentLoadReturnsResult:
    """load() 必须明确报告成败"""

    def test_load_returns_bool(self):
        """返回值必须是 bool，不能是 None"""
        agent = RLTradingAgent(model_type="ppo")
        result = agent.load("models/does_not_exist")

        assert isinstance(result, bool), (
            f"load() 应返回 bool 表示成败, 实际返回 {type(result).__name__} —— "
            f"调用方无法判断模型是否真的加载上"
        )

    def test_load_failure_returns_false(self):
        """加载失败必须返回 False"""
        agent = RLTradingAgent(model_type="ppo")
        assert agent.load("models/does_not_exist") is False
        assert agent.is_trained is False, "加载失败时 is_trained 必须为 False"

    def test_load_missing_file_returns_false(self):
        """文件不存在必须返回 False"""
        agent = RLTradingAgent(model_type="dqn")
        assert agent.load("/nonexistent/path/model") is False
        assert agent.is_trained is False

    def test_caller_can_branch_on_load_result(self):
        """回归的核心: 调用方确实能据此做分支

        这是修复前做不到的——返回值恒为 None，无论成败。
        """
        agent = RLTradingAgent(model_type="ppo")
        loaded = agent.load("models/does_not_exist")

        # 刻意保留 if/else 而非三元: 演示的正是「调用方依据返回值分支」这一场景
        if loaded:  # noqa: SIM108
            path_taken = "使用 RL 模型"
        else:
            path_taken = "降级到规则策略"

        assert path_taken == "降级到规则策略", "调用方应能根据 load() 返回值走降级分支"

    def test_analyze_still_works_after_failed_load(self, monkeypatch):
        """加载失败后 analyze 仍可降级工作，不崩溃

        记录现状：失败是静默的（只有日志），但功能不崩。
        本测试锁定「不崩溃」这一契约，防止修复时把降级路径也堵死。
        """
        import numpy as np
        import pandas as pd

        n = 120
        rng = np.random.default_rng(0)
        close = np.abs(10 + np.cumsum(rng.normal(0.02, 0.15, n))) + 5
        df = pd.DataFrame(
            {
                "open": close,
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "volume": rng.integers(1_000_000, 5_000_000, n).astype(float),
            },
            index=pd.date_range("2024-01-01", periods=n, freq="B"),
        )

        agent = RLTradingAgent(model_type="ppo")
        assert agent.load("models/does_not_exist") is False

        result = agent.analyze("000001", df)
        assert result is not None
        assert result.symbol == "000001"


# ========================================================================
# 相邻问题：假断言
# ========================================================================


class TestNoVacuousAssertions:
    """防止「三种状态都算通过」式的空断言回归"""

    def test_risk_exception_test_is_not_vacuous(self):
        """风控异常测试必须精确断言状态，不能用 in [...] 放宽"""
        from pathlib import Path

        test_file = Path(__file__).parent / "test_trading_pipeline.py"
        src = test_file.read_text(encoding="utf-8")

        # 找到 test_risk_check_exception_returns_empty 的函数体
        start = src.index("def test_risk_check_exception_returns_empty")
        body = src[start : start + 1200]
        # 截到下一个 def
        end = body.find("\n    def ", 10)
        if end > 0:
            body = body[:end]

        assert "PipelineStatus.BLOCKED," not in body, (
            "风控异常测试不应把三种状态都列为可接受 —— "
            "那等于没有断言。文件头注释宣称覆盖「风控拦截」,实际必须精确断言。"
        )
        assert "== PipelineStatus.EXECUTED" in body, "风控异常时应精确断言 EXECUTED"
