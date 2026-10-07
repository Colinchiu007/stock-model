"""模拟盘核心规则测试 (Phase 2)

覆盖 A 股交易规则与账户不变量。每条规则都对应真实约束,
不是形式化检查 —— 改坏任何一条都必须红。

设计原则: 用**构造数据**验证, 不依赖网络与真实行情。
"""

import pandas as pd
import pytest

from stock_model.paper.broker import (
    Broker,
    calc_fees,
    is_limit_down,
    is_limit_up,
    is_suspended,
    price_limit_pct,
)
from stock_model.paper.models import Account, Order, OrderStatus, Side

# ==================== 构造数据 ====================


def bar(open_=10.0, close=10.0, pct=0.0, vol=1_000_000, symbol="000002"):
    """构造一根日线 bar"""
    return pd.Series(
        {
            "open": open_,
            "high": max(open_, close) * 1.01,
            "low": min(open_, close) * 0.99,
            "close": close,
            "volume": vol,
            "pct_change": pct,
            "symbol": symbol,
        }
    )


def account(capital=10000.0):
    return Account(account_id="t", initial_capital=capital, cash=capital)


def order(side=Side.BUY, shares=100, symbol="000002", date="2026-01-05"):
    return Order(
        symbol=symbol,
        side=side,
        shares=shares,
        signal_action="BUY",
        signal_confidence=0.8,
        signal_reason="测试",
        created_date=date,
    )


# ==================== 费用计算 ====================


class TestFees:
    def test_buy_has_no_stamp_tax(self):
        """印花税仅卖出收取"""
        f = calc_fees(Side.BUY, 10000.0)
        assert f.stamp_tax == 0.0
        assert f.commission > 0

    def test_sell_has_stamp_tax(self):
        f = calc_fees(Side.SELL, 10000.0)
        # 印花税 = 0.05% = 5.0
        assert f.stamp_tax == pytest.approx(5.0, rel=0.01)
        # 佣金: 万3 = 3.0, 但有 5 元下限 -> 取 5.0
        assert f.commission == 5.0

    def test_commission_has_five_yuan_floor(self):
        """小额交易佣金不足5元时按5元收"""
        f = calc_fees(Side.BUY, 100.0)  # 10元 → 万3 只有0.03
        assert f.commission == 5.0

    def test_commission_above_floor_uses_rate(self):
        """大额按费率"""
        f = calc_fees(Side.BUY, 100000.0)
        assert f.commission == pytest.approx(30.0, rel=0.01)


# ==================== 涨跌停 ====================


class TestPriceLimit:
    def test_main_board_limit_10pct(self):
        assert price_limit_pct("000002") == 0.10
        assert price_limit_pct("600036") == 0.10

    def test_chinext_limit_20pct(self):
        assert price_limit_pct("300750") == 0.20
        assert price_limit_pct("301001") == 0.20

    def test_star_market_limit_20pct(self):
        assert price_limit_pct("688981") == 0.20

    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            ("000002", 0.10),
            ("600036", 0.10),
            ("300750", 0.20),
            ("301001", 0.20),
            ("688981", 0.20),
            ("689009", 0.20),
            ("830799", 0.10),  # 北交所
            ("sz300750", 0.20),  # 带市场前缀
            ("sh600036", 0.10),
        ],
    )
    def test_limit_pct_variants(self, code, expected):
        """含市场前缀与北交所的边界

        注意: 早期实现用 lstrip("szsh") 剥离前缀, 该 API 会按**字符集**删除
        而非按前缀, 对 "sz300..." 之外的输入有破坏风险(ruff B005 亦指此点)。
        """
        assert price_limit_pct(code) == expected

    def test_limit_up_detection(self):
        assert is_limit_up(bar(pct=10.0, symbol="000002")) is True
        assert is_limit_up(bar(pct=3.0, symbol="000002")) is False

    def test_chinext_limit_up_at_20pct(self):
        assert is_limit_up(bar(pct=20.0, symbol="300750")) is True
        assert is_limit_up(bar(pct=11.0, symbol="300750")) is False  # 主板幅度不算涨停

    def test_limit_down_detection(self):
        assert is_limit_down(bar(pct=-10.0, symbol="000002")) is True
        assert is_limit_down(bar(pct=-3.0, symbol="000002")) is False

    def test_suspended_when_zero_volume(self):
        assert is_suspended(bar(vol=0)) is True
        assert is_suspended(bar(vol=1000)) is False


# ==================== 买入撮合 ====================


class TestBuyExecution:
    def test_buy_deducts_amount_plus_fees(self):
        """买入扣款 = 金额 + 佣金 + 过户费"""
        acc = account(10000.0)
        br = Broker(acc)
        br.submit_order(order(shares=100))
        br.match(bar(open_=10.0), "2026-01-06")

        assert len(acc.trades) == 1
        t = acc.trades[0]
        assert t.amount == 1000.0
        expected_fee = calc_fees(Side.BUY, 1000.0).total
        assert acc.cash == pytest.approx(10000.0 - 1000.0 - expected_fee, abs=0.01)

    def test_buy_fills_at_open_price(self):
        """成交价必须是 T+1 开盘价, 而非信号日收盘价"""
        acc = account(10000.0)
        br = Broker(acc)
        br.submit_order(order(shares=100))
        br.match(bar(open_=12.5, close=15.0), "2026-01-06")
        assert acc.trades[0].price == 12.5, "必须用开盘价成交, 收盘价会造成未来函数"

    def test_buy_rounds_down_to_lot(self):
        """非整百买入向下取整"""
        acc = account(10000.0)
        br = Broker(acc)
        br.submit_order(order(shares=150))
        br.match(bar(open_=10.0), "2026-01-06")
        assert acc.trades[0].shares == 100

    def test_buy_rejects_below_one_lot(self):
        """不足100股直接拒单"""
        acc = account(10000.0)
        br = Broker(acc)
        o = order(shares=50)
        br.submit_order(o)
        br.match(bar(), "2026-01-06")
        assert o.status == OrderStatus.REJECTED
        assert len(acc.trades) == 0

    def test_buy_rejects_when_cash_insufficient(self):
        """资金不足拒单, 且现金不得为负"""
        acc = account(1000.0)  # 总共只有1000
        br = Broker(acc)
        br.submit_order(order(shares=1000))  # 需要1万
        br.match(bar(open_=10.0), "2026-01-06")
        assert len(acc.trades) == 0
        assert acc.cash == 1000.0, "现金不能变成负数"
        assert acc.cash >= 0, "账户不变量: 现金不得为负"

    def test_buy_shrinks_to_affordable_shares(self):
        """资金只够一部分时缩量成交而非全拒

        放开集中度限制, 以便单独验证「资金约束」这一条路径。
        """
        acc = account(2500.0)
        br = Broker(acc, max_position_pct=1.0)
        br.submit_order(order(shares=1000))  # 需 1万+, 只有2500
        br.match(bar(open_=10.0), "2026-01-06")
        assert len(acc.trades) == 1
        assert acc.trades[0].shares == 200  # 2500 够买200股
        assert acc.cash >= 0

    def test_buy_rejected_on_limit_up(self):
        """涨停日买单不成交"""
        acc = account(10000.0)
        br = Broker(acc)
        o = order(shares=100)
        br.submit_order(o)
        br.match(bar(open_=11.0, pct=10.0), "2026-01-06")
        assert o.status == OrderStatus.REJECTED
        assert "涨停" in o.reject_reason

    def test_buy_rejected_when_suspended(self):
        """停牌不成交"""
        acc = account(10000.0)
        br = Broker(acc)
        o = order(shares=100)
        br.submit_order(o)
        br.match(bar(vol=0), "2026-01-06")
        assert o.status == OrderStatus.REJECTED
        assert "停牌" in o.reject_reason


# ==================== T+1 限制（防未来函数的核心） ====================


class TestTPlusOne:
    def test_same_day_sell_is_rejected(self):
        """当日买入的股票当日不可卖出"""
        acc = account(10000.0)
        br = Broker(acc)
        br.submit_order(order(side=Side.BUY, shares=100))
        br.match(bar(open_=10.0), "2026-01-06")

        pos = acc.get_position("000002")
        assert pos.frozen_shares == 100, "当日买入应被冻结"

        # 同日尝试卖出
        br.submit_order(order(side=Side.SELL, shares=100, date="2026-01-06"))
        br.match(bar(open_=10.0), "2026-01-06")
        assert len(acc.trades) == 1, "T+1 内不应产生第二笔成交"
        assert pos.shares == 100, "持仓不应被卖出"

    def test_sell_allowed_next_day(self):
        """次日可正常卖出"""
        acc = account(10000.0)
        br = Broker(acc)
        br.submit_order(order(side=Side.BUY, shares=100))
        br.match(bar(open_=10.0), "2026-01-06")
        br.unfreeze_all()  # 隔夜解冻

        br.submit_order(order(side=Side.SELL, shares=100, date="2026-01-07"))
        br.match(bar(open_=11.0), "2026-01-07")
        assert len(acc.trades) == 2
        assert "000002" not in acc.positions, "应已清仓"


# ==================== 卖出与盈亏 ====================


class TestSellExecution:
    def test_sell_credits_cash_minus_stamp_tax(self):
        """卖出到账 = 金额 - 佣金 - 印花税 - 过户费"""
        acc = account(10000.0)
        br = Broker(acc)
        br.submit_order(order(side=Side.BUY, shares=100))
        br.match(bar(open_=10.0), "2026-01-06")
        br.unfreeze_all()

        cash_before = acc.cash
        br.submit_order(order(side=Side.SELL, shares=100))
        br.match(bar(open_=12.0), "2026-01-07")

        amount = 1200.0
        fees = calc_fees(Side.SELL, amount).total
        assert acc.cash == pytest.approx(cash_before + amount - fees, abs=0.01)

    def test_profit_loss_pct_correct(self):
        acc = account(10000.0)
        br = Broker(acc)
        br.submit_order(order(side=Side.BUY, shares=100))
        br.match(bar(open_=10.0), "2026-01-06")
        br.mark_to_market({"000002": 12.0}, "2026-01-06")

        pos = acc.get_position("000002")
        assert pos.avg_cost == pytest.approx(10.0, abs=0.01)
        assert pos.profit_loss_pct == pytest.approx(0.2, abs=0.01)

    def test_averaged_cost_after_second_buy(self):
        """移动加权平均成本

        覆盖三种情形: 新建仓 / 二次加仓 / 三次加仓。
        单测「只买一次」无法暴露加权逻辑缺陷 —— 因为新建仓时
        old_shares=0, 无论 avg_cost 预设为何都恰好得出 price,
        属变异测试发现不了的假保险。必须有多次加仓才能真正锁住公式。
        """
        acc = account(100000.0)
        br = Broker(acc, max_position_pct=1.0)
        br.submit_order(order(side=Side.BUY, shares=100))
        br.match(bar(open_=10.0), "2026-01-06")
        assert acc.get_position("000002").avg_cost == pytest.approx(10.0, abs=0.01)

        # 第二次: (100*10 + 100*20) / 200 = 15
        br.submit_order(order(side=Side.BUY, shares=100))
        br.match(bar(open_=20.0), "2026-01-07")
        pos = acc.get_position("000002")
        assert pos.avg_cost == pytest.approx(15.0, abs=0.01)
        assert pos.shares == 200

        # 第三次(数量不同): (200*15 + 300*30) / 500 = 24
        br.submit_order(order(side=Side.BUY, shares=300))
        br.match(bar(open_=30.0), "2026-01-08")
        pos = acc.get_position("000002")
        assert pos.avg_cost == pytest.approx(24.0, abs=0.01)
        assert pos.shares == 500

    def test_averaged_cost_mutation_sensitive(self):
        """显式锁定加权公式的形态

        防止未来把公式写成「按最新价覆盖均价」这类看似合理实则错误的实现。
        """
        acc = account(100000.0)
        br = Broker(acc, max_position_pct=1.0)
        br.submit_order(order(side=Side.BUY, shares=100))
        br.match(bar(open_=10.0), "2026-01-06")
        br.submit_order(order(side=Side.BUY, shares=100))
        br.match(bar(open_=20.0), "2026-01-07")

        pos = acc.get_position("000002")
        # 错误实现会得到 20.0(按最新价覆盖), 正确实现是 15.0
        assert pos.avg_cost == pytest.approx(15.0, abs=0.01), (
            f"均价应为移动加权(15.0), 实际 {pos.avg_cost} —— 若等于最新价 20.0 说明被错误覆盖"
        )

    def test_sell_rejected_on_limit_down(self):
        """跌停日卖单不成交"""
        acc = account(10000.0)
        br = Broker(acc)
        br.submit_order(order(side=Side.BUY, shares=100))
        br.match(bar(), "2026-01-06")
        br.unfreeze_all()

        o = order(side=Side.SELL, shares=100)
        br.submit_order(o)
        br.match(bar(open_=9.0, pct=-10.0), "2026-01-07")
        assert o.status == OrderStatus.REJECTED
        assert "跌停" in o.reject_reason

    def test_sell_rejected_without_position(self):
        acc = account(10000.0)
        br = Broker(acc)
        o = order(side=Side.SELL, shares=100)
        br.submit_order(o)
        br.match(bar(), "2026-01-06")
        assert o.status == OrderStatus.REJECTED


# ==================== 账户不变量 ====================


class TestAccountInvariants:
    def test_cash_never_negative(self):
        """核心不变量: 现金任何时刻不得为负"""
        acc = account(10000.0)
        br = Broker(acc)
        for i in range(20):
            br.submit_order(order(side=Side.BUY, shares=500))
            br.match(bar(open_=50.0), f"2026-01-{i + 1:02d}")
            br.unfreeze_all()
            assert acc.cash >= 0, f"第{i}轮现金为负: {acc.cash}"

    def test_total_asset_equals_cash_plus_market(self):
        """总资产 = 现金 + 持仓市值"""
        acc = account(10000.0)
        br = Broker(acc)
        br.submit_order(order(side=Side.BUY, shares=200))
        br.match(bar(open_=10.0), "2026-01-06")
        br.mark_to_market({"000002": 15.0}, "2026-01-06")

        assert acc.total_asset == pytest.approx(acc.cash + acc.market_value, abs=0.01)
        assert acc.total_asset == pytest.approx(acc.market_value + acc.cash, abs=0.01)

    def test_equity_curve_recorded(self):
        acc = account(10000.0)
        br = Broker(acc)
        br.mark_to_market({}, "2026-01-06")
        assert len(acc.equity_curve) == 1
        assert acc.equity_curve[0].total_asset == pytest.approx(10000.0, abs=0.01)

    def test_drawdown_tracked(self):
        acc = account(10000.0)
        br = Broker(acc)
        br.mark_to_market({}, "2026-01-06")  # 10000, peak=10000
        br.submit_order(order(side=Side.BUY, shares=500))
        br.match(bar(open_=10.0), "2026-01-07")
        br.mark_to_market({"000002": 5.0}, "2026-01-07")  # 亏一半
        assert acc.equity_curve[-1].drawdown < 0

    def test_serialization_roundtrip(self):
        acc = account(10000.0)
        br = Broker(acc)
        br.submit_order(order(side=Side.BUY, shares=100))
        br.match(bar(open_=10.0), "2026-01-06")

        restored = Account.from_json(acc.to_json())
        assert restored.cash == pytest.approx(acc.cash, abs=0.01)
        assert len(restored.trades) == 1
        assert restored.get_position("000002").shares == 100


# ==================== 集中度约束 ====================


class TestConcentrationLimit:
    def test_first_buy_is_constrained(self):
        """首次买入同样受 20% 约束

        这条曾失效: 约束判断包在 `if pos is not None` 内,
        新建仓时 pos 为 None 而整段被跳过, 实测买入 90% 未被拦截。
        """
        acc = account(10000.0)
        br = Broker(acc, max_position_pct=0.20)
        br.submit_order(order(side=Side.BUY, shares=900))  # 名义 9000 元
        br.match(bar(open_=10.0), "2026-01-06")

        pos = acc.get_position("000002")
        assert pos is not None, "应至少成交最小一手"
        ratio = pos.market_value / acc.total_asset
        assert ratio <= 0.20 + 0.005, f"首次买入集中度超限: {ratio:.2%}"

    def test_concentration_rejects_when_no_room(self):
        """小资金账户下 20% 上限买不出一手时直接拒单"""
        acc = account(500.0)  # 20% = 100 元 < 1手(10元×100=1000元)
        br = Broker(acc, max_position_pct=0.20)
        o = order(side=Side.BUY, shares=100)
        br.submit_order(o)
        br.match(bar(open_=10.0), "2026-01-06")
        assert o.status == OrderStatus.REJECTED
        assert len(acc.trades) == 0
        assert acc.cash == 500.0, "拒单后现金不应变动"
