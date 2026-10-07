"""绩效指标测试

用**构造数据**验证公式, 不依赖网络。
重点覆盖: 回撤、夏普、胜率、以及「样本不足必须警告」这条诚实性约束。
"""

import pytest

from stock_model.paper.metrics import (
    MIN_TRADES_FOR_RELIABILITY,
    PerformanceMetrics,
    _max_drawdown,
    _round_trips,
    evaluate,
)
from stock_model.paper.models import Account, EquityPoint, Side, Trade


def make_account(assets: list[float], initial: float = 10000.0) -> Account:
    """按给定的总资产序列构造带资金曲线的账户"""
    acc = Account(initial_capital=initial, cash=initial)
    for i, a in enumerate(assets):
        acc.equity_curve.append(
            EquityPoint(
                date=f"2026-01-{i + 1:02d}",
                total_asset=a,
                cash=a,
                market_value=0.0,
            )
        )
    acc.cash = assets[-1]
    return acc


def make_trade(side: Side, price: float, date: str, shares: int = 100) -> Trade:
    return Trade(
        trade_id=f"T{date}{side.value}{shares}",
        symbol="X",
        side=side,
        shares=shares,
        price=price,
        amount=price * shares,
        executed_at=date,
    )


# ==================== 回撤 ====================


class TestMaxDrawdown:
    def test_monotonic_up_has_no_drawdown(self):
        eq = [EquityPoint("d", a, a, 0) for a in (100, 110, 120)]
        dd, dur = _max_drawdown(eq)
        assert dd == 0.0
        assert dur == 0

    def test_simple_drawdown(self):
        # 100 -> 120 -> 90: 峰值120, 跌到90 = -25%
        eq = [EquityPoint("d", a, a, 0) for a in (100, 120, 90)]
        dd, dur = _max_drawdown(eq)
        assert dd == pytest.approx(-0.25, rel=0.01)
        assert dur == 1

    def test_duration_counts_underwater_days(self):
        # 峰值后连续 3 天未创新高
        eq = [EquityPoint("d", a, a, 0) for a in (100, 120, 118, 115, 110, 125)]
        dd, dur = _max_drawdown(eq)
        assert dur == 3, f"应记录连续 3 天在水下, 实际 {dur}"
        assert dd == pytest.approx((110 - 120) / 120, rel=0.01)

    def test_empty_and_single(self):
        assert _max_drawdown([]) == (0.0, 0)
        assert _max_drawdown([EquityPoint("d", 100, 100, 0)]) == (0.0, 0)


# ==================== 买卖回合配对 ====================


class TestRoundTrips:
    def test_single_pair(self):
        trips = _round_trips(
            [
                make_trade(Side.BUY, 10.0, "2026-01-01"),
                make_trade(Side.SELL, 12.0, "2026-01-10"),
            ]
        )
        assert len(trips) == 1
        assert trips[0]["pnl"] == pytest.approx(200.0, rel=0.01)  # (12-10)*100
        assert trips[0]["pnl_pct"] == pytest.approx(0.2, rel=0.01)

    def test_loss_pair(self):
        trips = _round_trips(
            [
                make_trade(Side.BUY, 10.0, "2026-01-01"),
                make_trade(Side.SELL, 8.0, "2026-01-10"),
            ]
        )
        assert trips[0]["pnl"] == pytest.approx(-200.0, rel=0.01)

    def test_partial_sale_splits_lots(self):
        """分批卖出应正确拆分回合"""
        trips = _round_trips(
            [
                make_trade(Side.BUY, 10.0, "2026-01-01", shares=100),
                make_trade(Side.SELL, 12.0, "2026-01-10", shares=50),
            ]
        )
        assert len(trips) == 1
        assert trips[0]["shares"] == 50

    def test_two_full_cycles(self):
        trips = _round_trips(
            [
                make_trade(Side.BUY, 10.0, "2026-01-01"),
                make_trade(Side.SELL, 11.0, "2026-01-10"),
                make_trade(Side.BUY, 11.0, "2026-02-01"),
                make_trade(Side.SELL, 13.0, "2026-02-10"),
            ]
        )
        assert len(trips) == 2
        assert trips[0]["pnl"] == pytest.approx(100.0, rel=0.01)
        assert trips[1]["pnl"] == pytest.approx(200.0, rel=0.01)

    def test_open_position_not_counted(self):
        """未平仓的持仓不构成完整回合"""
        trips = _round_trips([make_trade(Side.BUY, 10.0, "2026-01-01")])
        assert len(trips) == 0


# ==================== 整体绩效 ====================


class TestEvaluate:
    def test_total_return(self):
        acc = make_account([10000, 10500, 11000])
        m = evaluate(acc)
        assert m.total_return == pytest.approx(0.10, rel=0.01)

    def test_metrics_on_empty_account(self):
        """空账户不得抛异常, 全部返回 0"""
        m = evaluate(Account(initial_capital=10000, cash=10000))
        assert m.total_return == 0.0
        assert m.max_drawdown == 0.0
        assert m.sharpe == 0.0
        assert m.round_trips == 0

    def test_win_rate_computed_from_trips(self):
        acc = make_account([10000, 10000])
        acc.trades = [
            make_trade(Side.BUY, 10.0, "2026-01-01"),
            make_trade(Side.SELL, 12.0, "2026-01-10"),  # 赢
            make_trade(Side.BUY, 10.0, "2026-02-01"),
            make_trade(Side.SELL, 9.0, "2026-02-10"),  # 输
        ]
        m = evaluate(acc)
        assert m.round_trips == 2
        assert m.win_rate == pytest.approx(0.5, rel=0.01)
        assert m.total_profit > 0
        assert m.total_loss > 0

    def test_benchmark_comparison(self):
        acc = make_account([10000, 10500])  # 策略 +5%
        m = evaluate(acc, benchmark_prices=[100.0, 102.0])  # 基准 +2%
        assert m.benchmark_return == pytest.approx(0.02, rel=0.01)
        assert m.alpha == pytest.approx(0.03, rel=0.01)

    def test_fee_accumulation(self):
        acc = make_account([10000, 10000])
        t1 = make_trade(Side.BUY, 10.0, "2026-01-01")
        t1.commission = 5.0
        t1.stamp_tax = 0.0
        t1.transfer_fee = 0.01
        t2 = make_trade(Side.SELL, 10.0, "2026-01-10")
        t2.commission = 5.0
        t2.stamp_tax = 0.5
        t2.transfer_fee = 0.01
        acc.trades = [t1, t2]
        m = evaluate(acc)
        assert m.total_fee == pytest.approx(10.52, rel=0.01)


# ==================== 诚实性约束（核心） ====================


class TestReliabilityGuard:
    """样本不足时必须警告, 不许静默给出"结论"

    这是本模块最重要的约束 —— 用 3 笔交易宣称 67% 胜率
    会让使用者产生严重误判。
    """

    def test_always_unreliable_with_zero_trades(self):
        m = evaluate(make_account([10000, 10000]))
        assert m.reliable is False
        assert m.warnings, "0 回合必须给出警告"

    def test_unreliable_below_threshold(self):
        acc = make_account([10000, 10000])
        n = MIN_TRADES_FOR_RELIABILITY - 1
        for i in range(n):
            acc.trades += [
                make_trade(Side.BUY, 10.0, f"2026-01-{i + 1:02d}"),
                make_trade(Side.SELL, 11.0, f"2026-02-{i + 1:02d}"),
            ]
        m = evaluate(acc)
        assert m.round_trips == n
        assert m.reliable is False
        assert any("样本" in w or "不足" in w for w in m.warnings)

    def test_reliable_above_threshold(self):
        acc = make_account([10000, 10000])
        n = MIN_TRADES_FOR_RELIABILITY + 2
        for i in range(n):
            acc.trades += [
                make_trade(Side.BUY, 10.0, f"2026-01-{i + 1:02d}"),
                make_trade(Side.SELL, 11.0, f"2026-02-{i + 1:02d}"),
            ]
        m = evaluate(acc)
        assert m.round_trips > MIN_TRADES_FOR_RELIABILITY
        assert m.reliable is True

    def test_warning_text_is_actionable(self):
        """警告必须说清楚「别据此下结论」, 而不只是报个数字"""
        m = evaluate(make_account([10000, 10000]))
        text = " ".join(m.warnings)
        assert "20" in text or "少于" in text
        assert any(k in text for k in ("意义", "不足", "勿", "不要"))

    def test_short_period_warning(self):
        """交易日过少时也该提醒年化不可靠"""
        acc = make_account([10000, 10100, 10200])  # 仅 3 天
        m = evaluate(acc)
        assert any("交易日" in w for w in m.warnings)

    def test_metrics_to_dict_exposes_reliability(self):
        """可靠性标记必须出现在序列化结果里, 否则前端拿不到"""
        d = evaluate(make_account([10000, 10000])).to_dict()
        assert "reliable" in d
        assert "warnings" in d
        assert d["reliable"] is False
        assert isinstance(d["warnings"], list)

    def test_dataclass_str_marks_unreliable(self):
        m = PerformanceMetrics(round_trips=3)
        assert "样本不足" in str(m)
        m2 = PerformanceMetrics(round_trips=30, reliable=True)
        assert "样本不足" not in str(m2)
