"""
风险管理模块单元测试
"""

import pytest

from stock_model.risk.manager import RiskManager
from stock_model.risk.models import (
    Position,
    PortfolioRisk,
    RiskAlert,
    RiskLevel,
    RiskType,
)
from stock_model.risk.position_sizer import PositionSizer


# --- Position 测试 ---

class TestPosition:
    def test_market_value(self):
        pos = Position(symbol="000001", shares=1000, current_price=10.0)
        assert pos.market_value == 10000.0

    def test_cost_value(self):
        pos = Position(symbol="000001", shares=1000, cost_price=9.0)
        assert pos.cost_value == 9000.0

    def test_profit_loss(self):
        pos = Position(
            symbol="000001", shares=1000, cost_price=9.0, current_price=10.0
        )
        assert pos.profit_loss == 1000.0

    def test_profit_loss_pct(self):
        pos = Position(
            symbol="000001", shares=1000, cost_price=9.0, current_price=10.0
        )
        assert pos.profit_loss_pct == pytest.approx(1.0 / 9.0, abs=0.001)

    def test_profit_loss_pct_zero_cost(self):
        pos = Position(symbol="000001", shares=1000, cost_price=0.0, current_price=10.0)
        assert pos.profit_loss_pct == 0.0

    def test_str(self):
        pos = Position(
            symbol="000001", shares=1000, cost_price=9.0, current_price=10.0
        )
        assert "000001" in str(pos)


# --- RiskAlert 测试 ---

class TestRiskAlert:
    def test_alert_str(self):
        alert = RiskAlert(
            level=RiskLevel.WARNING,
            type=RiskType.DRAWDOWN,
            message="回撤警告",
            action="密切关注",
        )
        assert "warning" in str(alert)
        assert "drawdown" in str(alert)


# --- RiskLevel/RiskType 测试 ---

class TestRiskEnums:
    def test_risk_levels(self):
        assert RiskLevel.LOW.value == "low"
        assert RiskLevel.WARNING.value == "warning"
        assert RiskLevel.DANGER.value == "danger"
        assert RiskLevel.CRITICAL.value == "critical"

    def test_risk_types(self):
        assert RiskType.DRAWDOWN.value == "drawdown"
        assert RiskType.CONCENTRATION.value == "concentration"
        assert RiskType.STOP_LOSS.value == "stop_loss"
        assert RiskType.TAKE_PROFIT.value == "take_profit"


# --- RiskManager 测试 ---

class TestRiskManager:
    def test_no_alerts_when_no_risk(self):
        rm = RiskManager()
        pos = Position(
            symbol="000001", shares=1000, cost_price=10.0, current_price=12.0
        )
        alerts = rm.check_portfolio_risk([pos], total_value=100000)
        # 正常持仓不应有警报(未触发止损止盈和集中度)
        critical_alerts = [a for a in alerts if a.level == RiskLevel.CRITICAL]
        assert len(critical_alerts) == 0

    def test_stop_loss_triggered(self):
        rm = RiskManager(default_stop_loss_pct=0.08)
        pos = Position(
            symbol="000001",
            shares=1000,
            cost_price=10.0,
            current_price=9.0,  # 亏损10% > 8%止损线
        )
        alert = rm.check_stop_loss(pos)
        assert alert is not None
        assert alert.level == RiskLevel.CRITICAL
        assert alert.type == RiskType.STOP_LOSS

    def test_stop_loss_approaching(self):
        rm = RiskManager(default_stop_loss_pct=0.08)
        pos = Position(
            symbol="000001",
            shares=1000,
            cost_price=10.0,
            current_price=9.3,  # 亏损7%，接近8%止损线
        )
        alert = rm.check_stop_loss(pos)
        assert alert is not None
        assert alert.level == RiskLevel.WARNING

    def test_stop_loss_not_triggered(self):
        rm = RiskManager(default_stop_loss_pct=0.08)
        pos = Position(
            symbol="000001",
            shares=1000,
            cost_price=10.0,
            current_price=10.5,  # 盈利5%
        )
        alert = rm.check_stop_loss(pos)
        assert alert is None

    def test_take_profit_triggered(self):
        rm = RiskManager(default_take_profit_pct=0.20)
        pos = Position(
            symbol="000001",
            shares=1000,
            cost_price=10.0,
            current_price=12.5,  # 盈利25% > 20%止盈线
        )
        alert = rm.check_take_profit(pos)
        assert alert is not None
        assert alert.type == RiskType.TAKE_PROFIT

    def test_take_profit_not_triggered(self):
        rm = RiskManager(default_take_profit_pct=0.20)
        pos = Position(
            symbol="000001",
            shares=1000,
            cost_price=10.0,
            current_price=11.0,  # 盈利10%
        )
        alert = rm.check_take_profit(pos)
        assert alert is None

    def test_drawdown_critical(self):
        rm = RiskManager(max_drawdown_limit=0.15)
        pos = Position(symbol="000001", shares=1000, cost_price=10.0, current_price=10.0)
        alerts = rm.check_portfolio_risk([pos], total_value=100000, current_drawdown=0.16)

        drawdown_alerts = [a for a in alerts if a.type == RiskType.DRAWDOWN]
        assert any(a.level == RiskLevel.CRITICAL for a in drawdown_alerts)

    def test_drawdown_warning(self):
        rm = RiskManager(max_drawdown_limit=0.15)
        pos = Position(symbol="000001", shares=1000, cost_price=10.0, current_price=10.0)
        alerts = rm.check_portfolio_risk([pos], total_value=100000, current_drawdown=0.08)

        drawdown_alerts = [a for a in alerts if a.type == RiskType.DRAWDOWN]
        assert any(a.level == RiskLevel.WARNING for a in drawdown_alerts)

    def test_concentration_alert(self):
        rm = RiskManager(position_concentration_limit=0.20)
        # 单股占总市值30% > 20%限制
        pos = Position(symbol="000001", shares=1000, cost_price=10.0, current_price=10.0)
        alerts = rm.check_portfolio_risk([pos], total_value=33333)

        concentration_alerts = [a for a in alerts if a.type == RiskType.CONCENTRATION]
        assert len(concentration_alerts) > 0

    def test_empty_positions(self):
        rm = RiskManager()
        alerts = rm.check_portfolio_risk([], total_value=0)
        assert alerts == []

    def test_zero_shares_position(self):
        rm = RiskManager()
        pos = Position(symbol="000001", shares=0)
        alerts = rm.check_position_risk(pos)
        assert alerts == []


# --- PositionSizer 测试 ---

class TestPositionSizer:
    def test_fixed_size(self):
        sizer = PositionSizer()
        shares = sizer.fixed_size(capital=100000, price=10.0, position_pct=0.10)
        assert shares == 1000  # 10000 / 10 = 1000股

    def test_fixed_size_respects_max(self):
        sizer = PositionSizer(max_position_pct=0.05)
        shares = sizer.fixed_size(capital=100000, price=10.0, position_pct=0.10)
        # 仓位比例被限制到5%，即5000元，500股
        assert shares == 500

    def test_fixed_size_min_shares(self):
        sizer = PositionSizer(min_shares=100)
        shares = sizer.fixed_size(capital=500, price=10.0, position_pct=0.10)
        # 50元不够买100股
        assert shares == 0

    def test_kelly_size_positive(self):
        sizer = PositionSizer()
        shares = sizer.kelly_size(
            capital=100000,
            price=10.0,
            win_rate=0.6,
            avg_win=0.05,
            avg_loss=0.03,
        )
        assert shares > 0

    def test_kelly_size_negative_returns_zero(self):
        sizer = PositionSizer()
        # 胜率太低，凯利公式建议不买入
        shares = sizer.kelly_size(
            capital=100000,
            price=10.0,
            win_rate=0.2,
            avg_win=0.02,
            avg_loss=0.05,
        )
        assert shares == 0

    def test_kelly_size_invalid_params(self):
        sizer = PositionSizer()
        # avg_loss=0无效，回退到固定仓位
        shares = sizer.kelly_size(
            capital=100000,
            price=10.0,
            win_rate=0.6,
            avg_win=0.05,
            avg_loss=0.0,
        )
        assert shares >= 0  # 回退到固定仓位

    def test_risk_parity(self):
        sizer = PositionSizer()
        prices = {"000001": 10.0, "600000": 20.0}
        volatilities = {"000001": 0.02, "600000": 0.04}

        result = sizer.risk_parity(capital=100000, prices=prices, volatilities=volatilities)

        assert "000001" in result
        assert "600000" in result
        # 低波动率应该分配更多仓位
        assert result["000001"] >= result["600000"]

    def test_risk_parity_empty(self):
        sizer = PositionSizer()
        result = sizer.risk_parity(capital=100000, prices={}, volatilities={})
        assert result == {}

    def test_atr_size(self):
        sizer = PositionSizer()
        shares = sizer.atr_size(
            capital=100000,
            price=10.0,
            atr=0.5,
            risk_pct=0.02,
            atr_multiplier=2.0,
        )
        # 止损距离=0.5*2=1.0, 风险金额=2000, 股数=2000/1.0=2000
        assert shares > 0

    def test_atr_size_zero_atr(self):
        sizer = PositionSizer()
        shares = sizer.atr_size(capital=100000, price=10.0, atr=0.0)
        assert shares == 0

    def test_atr_size_respects_max(self):
        sizer = PositionSizer(max_position_pct=0.05)
        shares = sizer.atr_size(
            capital=100000,
            price=10.0,
            atr=0.1,  # 很小的ATR，理论上会算出很大仓位
            risk_pct=0.02,
            atr_multiplier=2.0,
        )
        # 但被max_position_pct限制
        max_shares = int(100000 * 0.05 / 10.0 / 100) * 100
        assert shares <= max_shares


# --- PortfolioRisk 测试 ---

class TestPortfolioRisk:
    def test_max_concentration(self):
        positions = [
            Position(symbol="000001", shares=1000, current_price=10.0),
            Position(symbol="600000", shares=500, current_price=20.0),
        ]
        risk = PortfolioRisk(total_value=20000, positions=positions)
        # 000001: 10000/20000 = 50%
        assert risk.max_concentration == pytest.approx(0.5, abs=0.01)

    def test_max_concentration_empty(self):
        risk = PortfolioRisk(total_value=0, positions=[])
        assert risk.max_concentration == 0.0

    def test_has_critical_alerts(self):
        risk = PortfolioRisk(
            alerts=[
                RiskAlert(level=RiskLevel.CRITICAL, type=RiskType.DRAWDOWN, message="test")
            ]
        )
        assert risk.has_critical_alerts

    def test_no_critical_alerts(self):
        risk = PortfolioRisk(
            alerts=[
                RiskAlert(level=RiskLevel.WARNING, type=RiskType.DRAWDOWN, message="test")
            ]
        )
        assert not risk.has_critical_alerts