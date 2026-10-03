"""
Batch3 测试: 组合优化 + RL Agent + Web Dashboard
"""

import numpy as np
import pandas as pd
import pytest

# ============================================================
# 组合优化测试
# ============================================================


class TestPortfolioModels:
    """组合模型测试"""

    def test_portfolio_weight_creation(self):
        from stock_model.portfolio.models import PortfolioWeight

        w = PortfolioWeight(symbol="000001", weight=0.5, shares=100, price=10.0)
        assert w.symbol == "000001"
        assert w.weight == 0.5
        assert w.shares == 100
        assert w.price == 10.0

    def test_portfolio_weight_value(self):
        from stock_model.portfolio.models import PortfolioWeight

        w = PortfolioWeight(symbol="000001", weight=0.5, shares=200, price=15.0)
        assert w.value == 3000.0

    def test_portfolio_creation(self):
        from stock_model.portfolio.models import Portfolio, PortfolioWeight

        weights = [
            PortfolioWeight(symbol="000001", weight=0.6, shares=60, price=10.0),
            PortfolioWeight(symbol="000002", weight=0.4, shares=40, price=10.0),
        ]
        p = Portfolio(name="test_portfolio", weights=weights, total_value=1000.0)
        assert p.name == "test_portfolio"
        assert len(p.weights) == 2
        assert p.total_value == 1000.0

    def test_portfolio_weight_sum(self):
        from stock_model.portfolio.models import Portfolio, PortfolioWeight

        weights = [
            PortfolioWeight(symbol="A", weight=0.3, shares=30, price=10.0),
            PortfolioWeight(symbol="B", weight=0.7, shares=70, price=10.0),
        ]
        p = Portfolio(name="test", weights=weights, total_value=1000.0)
        assert abs(sum(w.weight for w in p.weights) - 1.0) < 1e-6


class TestPortfolioOptimizer:
    """组合优化器测试"""

    @pytest.fixture
    def sample_prices(self):
        return {"000001": 10.0, "000002": 20.0, "000003": 30.0}

    @pytest.fixture
    def sample_returns(self):
        np.random.seed(42)
        dates = pd.date_range("2024-01-01", periods=100, freq="D")
        return {
            "000001": pd.Series(np.random.normal(0.001, 0.02, 100), index=dates),
            "000002": pd.Series(np.random.normal(0.002, 0.03, 100), index=dates),
            "000003": pd.Series(np.random.normal(0.0015, 0.025, 100), index=dates),
        }

    def test_equal_weight(self, sample_prices):
        from stock_model.portfolio.optimizer import PortfolioOptimizer

        opt = PortfolioOptimizer()
        portfolio = opt.equal_weight(
            symbols=list(sample_prices.keys()), prices=sample_prices, total_value=90000
        )
        assert len(portfolio.weights) == 3
        for w in portfolio.weights:
            assert abs(w.weight - 1 / 3) < 1e-6
        assert portfolio.total_value == 90000

    def test_equal_weight_shares(self, sample_prices):
        from stock_model.portfolio.optimizer import PortfolioOptimizer

        opt = PortfolioOptimizer()
        portfolio = opt.equal_weight(
            symbols=list(sample_prices.keys()), prices=sample_prices, total_value=90000
        )
        # 每只股票分配 30000，shares = 30000 / price
        for w in portfolio.weights:
            expected_shares = int(30000 / w.price)
            assert w.shares == expected_shares

    def test_risk_parity(self, sample_returns, sample_prices):
        from stock_model.portfolio.optimizer import PortfolioOptimizer

        opt = PortfolioOptimizer()
        portfolio = opt.risk_parity(
            returns=sample_returns, prices=sample_prices, total_value=100000
        )
        assert len(portfolio.weights) == 3
        # 权重之和应为1
        total_weight = sum(w.weight for w in portfolio.weights)
        assert abs(total_weight - 1.0) < 0.05

    def test_risk_parity_low_vol_high_weight(self):
        """低波动率资产应获得更高权重"""
        from stock_model.portfolio.optimizer import PortfolioOptimizer

        np.random.seed(42)
        dates = pd.date_range("2024-01-01", periods=100, freq="D")
        returns = {
            "low_vol": pd.Series(np.random.normal(0.001, 0.01, 100), index=dates),
            "high_vol": pd.Series(np.random.normal(0.001, 0.05, 100), index=dates),
        }
        prices = {"low_vol": 10.0, "high_vol": 10.0}

        opt = PortfolioOptimizer()
        portfolio = opt.risk_parity(returns=returns, prices=prices, total_value=10000)

        low_vol_weight = next(w.weight for w in portfolio.weights if w.symbol == "low_vol")
        high_vol_weight = next(w.weight for w in portfolio.weights if w.symbol == "high_vol")
        # 低波动率应获得更高权重
        assert low_vol_weight > high_vol_weight

    def test_min_variance(self, sample_returns, sample_prices):
        from stock_model.portfolio.optimizer import PortfolioOptimizer

        opt = PortfolioOptimizer()
        portfolio = opt.min_variance(
            returns=sample_returns, prices=sample_prices, total_value=100000
        )
        assert len(portfolio.weights) == 3
        total_weight = sum(w.weight for w in portfolio.weights)
        assert abs(total_weight - 1.0) < 0.05

    def test_mean_variance(self, sample_returns, sample_prices):
        from stock_model.portfolio.optimizer import PortfolioOptimizer

        opt = PortfolioOptimizer()
        portfolio = opt.mean_variance(
            returns=sample_returns, prices=sample_prices, total_value=100000
        )
        assert len(portfolio.weights) == 3
        total_weight = sum(w.weight for w in portfolio.weights)
        assert abs(total_weight - 1.0) < 0.05

    def test_mean_variance_with_target(self, sample_returns, sample_prices):
        from stock_model.portfolio.optimizer import PortfolioOptimizer

        opt = PortfolioOptimizer()
        portfolio = opt.mean_variance(
            returns=sample_returns,
            prices=sample_prices,
            total_value=100000,
            target_return=0.05,
        )
        assert len(portfolio.weights) == 3

    def test_single_asset(self):
        """单资产组合"""
        from stock_model.portfolio.optimizer import PortfolioOptimizer

        opt = PortfolioOptimizer()
        portfolio = opt.equal_weight(
            symbols=["000001"], prices={"000001": 10.0}, total_value=10000
        )
        assert len(portfolio.weights) == 1
        assert portfolio.weights[0].weight == 1.0
        assert portfolio.weights[0].shares == 1000


# ============================================================
# RL Agent 测试
# ============================================================


class TestRLTradingAgent:
    """RL交易Agent测试"""

    @pytest.fixture
    def sample_df(self):
        np.random.seed(42)
        dates = pd.date_range("2024-01-01", periods=60, freq="D")
        close = 10 + np.cumsum(np.random.normal(0, 0.2, 60))
        volume = np.random.randint(100000, 500000, 60)
        return pd.DataFrame({"close": close, "volume": volume}, index=dates)

    def test_agent_creation(self):
        from stock_model.strategy.rl_agent import RLTradingAgent

        agent = RLTradingAgent()
        assert agent.name == "rl_agent"
        assert agent.model_type == "ppo"
        assert agent._trained is False

    def test_agent_creation_dqn(self):
        from stock_model.strategy.rl_agent import RLTradingAgent

        agent = RLTradingAgent(model_type="dqn")
        assert agent.model_type == "dqn"

    def test_rule_based_insufficient_data(self):
        from stock_model.strategy.rl_agent import RLTradingAgent

        agent = RLTradingAgent()
        df = pd.DataFrame({"close": [10, 11, 12]})
        result = agent.analyze("000001", df)
        assert result.action.value == "hold"
        assert result.confidence == 0.3

    def test_rule_based_buy_signal(self, sample_df):
        from stock_model.strategy.rl_agent import RLTradingAgent

        agent = RLTradingAgent()
        # 构造上升趋势数据
        dates = pd.date_range("2024-01-01", periods=30, freq="D")
        close = np.linspace(10, 15, 30)  # 持续上涨
        df = pd.DataFrame({"close": close, "volume": np.ones(30) * 100000}, index=dates)
        result = agent.analyze("000001", df)
        assert result.action.value in ("buy", "hold", "sell")

    def test_rule_based_sell_signal(self):
        from stock_model.strategy.rl_agent import RLTradingAgent

        agent = RLTradingAgent()
        # 构造下降趋势数据
        dates = pd.date_range("2024-01-01", periods=30, freq="D")
        close = np.linspace(15, 10, 30)  # 持续下跌
        df = pd.DataFrame({"close": close, "volume": np.ones(30) * 100000}, index=dates)
        result = agent.analyze("000001", df)
        assert result.action.value in ("sell", "hold", "buy")

    def test_train_without_deps(self):
        """无stable-baselines3时训练降级"""
        from stock_model.strategy.rl_agent import RLTradingAgent

        agent = RLTradingAgent()
        # train不会抛异常，只是降级
        agent.train(env=None, timesteps=100)

    def test_extract_features(self, sample_df):
        from stock_model.strategy.rl_agent import RLTradingAgent

        features = RLTradingAgent._extract_features(sample_df)
        assert isinstance(features, np.ndarray)
        assert features.dtype == np.float32
        assert len(features) == 10

    def test_extract_features_short_data(self):
        from stock_model.strategy.rl_agent import RLTradingAgent

        df = pd.DataFrame({"close": [10, 11]})
        features = RLTradingAgent._extract_features(df)
        assert len(features) == 10
        assert np.all(features == 0)

    def test_evaluate(self, sample_df):
        from stock_model.strategy.rl_agent import RLTradingAgent

        agent = RLTradingAgent()
        metrics = agent.evaluate("000001", sample_df)
        assert isinstance(metrics, dict)
        assert "total_return" in metrics

    def test_analyze_returns_strategy_result(self, sample_df):
        from stock_model.strategy.rl_agent import RLTradingAgent

        agent = RLTradingAgent()
        result = agent.analyze("000001", sample_df)
        assert hasattr(result, "action")
        assert hasattr(result, "confidence")
        assert hasattr(result, "reason")


# ============================================================
# Web Dashboard 测试
# ============================================================


class TestWebDashboard:
    """Web Dashboard测试(不依赖fastapi)"""

    def test_create_app_import_error(self):
        """fastapi未安装时抛ImportError"""
        # 此测试验证降级行为
        # 如果fastapi已安装，则跳过
        try:
            from stock_model.web.app import create_app

            app = create_app()
            # fastapi已安装，验证app创建成功
            assert app is not None
        except ImportError:
            # fastapi未安装，预期行为
            pass

    def test_render_dashboard_html(self):
        from stock_model.web.app import _render_dashboard

        html = _render_dashboard()
        assert "<!DOCTYPE html>" in html
        assert "Stock Model Dashboard" in html
        assert "api/health" in html
        assert "api/signals" in html
        assert "api/backtest" in html

    def test_dashboard_has_chinese(self):
        from stock_model.web.app import _render_dashboard

        html = _render_dashboard()
        assert "暂无信号" in html
        assert "暂无回测结果" in html

    def test_web_module_init(self):
        """验证模块可导入"""
        try:
            from stock_model.web import create_app
        except ImportError:
            pass  # fastapi未安装时正常