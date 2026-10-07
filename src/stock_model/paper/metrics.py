"""绩效指标计算

从账户的资金曲线与成交记录计算策略表现。

**诚实性要求**
------------
样本太少时结论不可信 —— 3 笔交易做出的"胜率 67%"没有意义。
``evaluate()`` 因此强制返回 ``reliable`` 标记与 ``warnings``,
由调用方（Web UI / API）显著展示，避免用小样本自我欺骗。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from itertools import pairwise
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from stock_model.paper.models import Account, Trade

TRADING_DAYS_PER_YEAR = 252
MIN_TRADES_FOR_RELIABILITY = 20  # 少于该笔数不做结论


@dataclass
class PerformanceMetrics:
    """策略绩效"""

    total_return: float = 0.0  # 累计收益率
    annual_return: float = 0.0  # 年化收益率
    max_drawdown: float = 0.0  # 最大回撤(负数)
    max_drawdown_duration: int = 0  # 最大回撤持续天数
    sharpe: float = 0.0  # 夏普比率
    sortino: float = 0.0  # 索提诺比率
    volatility: float = 0.0  # 年化波动率
    total_trades: int = 0  # 总成交笔数
    round_trips: int = 0  # 完整买卖回合数
    win_rate: float = 0.0  # 胜率
    profit_loss_ratio: float = 0.0  # 盈亏比
    avg_win: float = 0.0  # 平均盈利
    avg_loss: float = 0.0  # 平均亏损
    total_fee: float = 0.0  # 总费用
    total_profit: float = 0.0  # 累计盈利
    total_loss: float = 0.0  # 累计亏损
    final_asset: float = 0.0  # 期末总资产
    trading_days: int = 0  # 交易天数

    # 基准对比
    benchmark_return: float = 0.0  # 买入持有收益
    alpha: float = 0.0  # 超额收益
    beta: float = 0.0  # 系统性风险

    # 可靠性
    reliable: bool = False  # 样本是否足够下结论
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_return": round(self.total_return, 4),
            "annual_return": round(self.annual_return, 4),
            "max_drawdown": round(self.max_drawdown, 4),
            "max_drawdown_duration": self.max_drawdown_duration,
            "sharpe": round(self.sharpe, 3),
            "sortino": round(self.sortino, 3),
            "volatility": round(self.volatility, 4),
            "total_trades": self.total_trades,
            "round_trips": self.round_trips,
            "win_rate": round(self.win_rate, 4),
            "profit_loss_ratio": round(self.profit_loss_ratio, 3),
            "avg_win": round(self.avg_win, 4),
            "avg_loss": round(self.avg_loss, 4),
            "total_fee": round(self.total_fee, 2),
            "total_profit": round(self.total_profit, 2),
            "total_loss": round(self.total_loss, 2),
            "final_asset": round(self.final_asset, 2),
            "trading_days": self.trading_days,
            "benchmark_return": round(self.benchmark_return, 4),
            "alpha": round(self.alpha, 4),
            "beta": round(self.beta, 3),
            "reliable": self.reliable,
            "warnings": self.warnings,
        }

    def __str__(self) -> str:
        return (
            f"PerformanceMetrics(收益={self.total_return:.2%}, "
            f"年化={self.annual_return:.2%}, "
            f"最大回撤={self.max_drawdown:.2%}, "
            f"夏普={self.sharpe:.2f}, "
            f"胜率={self.win_rate:.2%}, "
            f"交易={self.round_trips}回合"
            f"{'' if self.reliable else ' [样本不足]'})"
        )


def _daily_returns(equity: list) -> list[float]:
    """从资金曲线算日收益率序列"""
    if len(equity) < 2:
        return []
    assets = [p.total_asset for p in equity]
    rets = []
    for prev, cur in pairwise(assets):
        if prev > 0:
            rets.append((cur - prev) / prev)
    return rets


def _max_drawdown(equity: list) -> tuple[float, int]:
    """最大回撤及其持续天数

    Returns:
        (最大回撤(负数), 持续天数)
    """
    if len(equity) < 2:
        return 0.0, 0
    peak = equity[0].total_asset
    max_dd = 0.0
    duration = 0
    cur_duration = 0
    for point in equity:
        if point.total_asset > peak:
            peak = point.total_asset
            cur_duration = 0
        else:
            # 等于峰值时属"尚未下跌", 不应计入水下持续天数。
            # 缺此判断会使单调上涨的曲线也报出 duration=1(实测复现)。
            if point.total_asset >= peak:
                continue
            cur_duration += 1
            if peak > 0:
                dd = (point.total_asset - peak) / peak
                max_dd = min(max_dd, dd)
            duration = max(duration, cur_duration)
    return max_dd, duration


def _round_trips(trades: list[Trade]) -> list[dict[str, Any]]:
    """把成交序列配对成完整买卖回合

    买入开仓、卖出平仓, 逐笔计算盈亏。
    """
    trips: list[dict[str, Any]] = []
    open_lots: dict[str, list[dict[str, Any]]] = {}

    for t in trades:
        if t.side.value == "buy":
            open_lots.setdefault(t.symbol, []).append(
                {"price": t.price, "shares": t.shares, "date": t.executed_at}
            )
        else:
            lots = open_lots.get(t.symbol, [])
            remain = t.shares
            while lots and remain > 0:
                lot = lots[0]
                matched = min(remain, lot["shares"])
                cost = lot["price"] * matched
                revenue = t.price * matched
                trips.append(
                    {
                        "symbol": t.symbol,
                        "open_date": lot["date"],
                        "close_date": t.executed_at,
                        "shares": matched,
                        "open_price": lot["price"],
                        "close_price": t.price,
                        "pnl": revenue - cost,
                        "pnl_pct": (t.price - lot["price"]) / lot["price"] if lot["price"] else 0.0,
                    }
                )
                lot["shares"] -= matched
                remain -= matched
                if lot["shares"] == 0:
                    lots.pop(0)
    return trips


def evaluate(
    account: Account,
    benchmark_prices: list[float] | None = None,
) -> PerformanceMetrics:
    """计算账户绩效

    Args:
        account: 模拟账户
        benchmark_prices: 基准价格序列(等长于资金曲线), 用于计算买入持有收益

    Returns:
        绩效指标(含可靠性标记)
    """
    m = PerformanceMetrics()
    equity = account.equity_curve
    m.trading_days = len(equity)
    m.total_trades = len(account.trades)
    m.total_fee = sum(t.total_fee for t in account.trades)
    m.final_asset = account.total_asset

    if account.initial_capital > 0:
        m.total_return = (account.total_asset - account.initial_capital) / (account.initial_capital)

    # 回撤
    m.max_drawdown, m.max_drawdown_duration = _max_drawdown(equity)

    # 收益风险指标
    rets = _daily_returns(equity)
    if len(rets) >= 2:
        mean_r = sum(rets) / len(rets)
        var = sum((r - mean_r) ** 2 for r in rets) / (len(rets) - 1)
        std = math.sqrt(var)
        m.volatility = std * math.sqrt(TRADING_DAYS_PER_YEAR)
        m.sharpe = mean_r / std * math.sqrt(TRADING_DAYS_PER_YEAR) if std > 0 else 0.0

        downside = [r for r in rets if r < 0]
        if len(downside) >= 2:
            d_var = sum(r**2 for r in downside) / (len(downside) - 1)
            d_std = math.sqrt(d_var)
            if d_std > 0:
                m.sortino = mean_r / d_std * math.sqrt(TRADING_DAYS_PER_YEAR)
        else:
            m.sortino = 0.0

    # 年化
    if m.trading_days >= 2 and account.initial_capital > 0 and account.total_asset > 0:
        years = m.trading_days / TRADING_DAYS_PER_YEAR
        growth = account.total_asset / account.initial_capital
        if growth > 0:
            m.annual_return = growth ** (1 / years) - 1

    # 买卖回合
    trips = _round_trips(account.trades)
    m.round_trips = len(trips)
    if trips:
        wins = [t for t in trips if t["pnl"] > 0]
        losses = [t for t in trips if t["pnl"] <= 0]
        m.win_rate = len(wins) / len(trips)
        m.total_profit = sum(t["pnl"] for t in wins)
        m.total_loss = abs(sum(t["pnl"] for t in losses))
        m.avg_win = m.total_profit / len(wins) if wins else 0.0
        m.avg_loss = m.total_loss / len(losses) if losses else 0.0
        m.profit_loss_ratio = m.avg_win / m.avg_loss if m.avg_loss > 0 else 0.0

    # 基准对比
    if benchmark_prices and len(benchmark_prices) >= 2 and benchmark_prices[0] > 0:
        m.benchmark_return = benchmark_prices[-1] / benchmark_prices[0] - 1
        m.alpha = m.total_return - m.benchmark_return
        if len(rets) >= 2 and m.benchmark_return != 0:
            # 简化 Beta: 用两者的协方差 / 基准方差
            bm_rets = [
                (benchmark_prices[i + 1] - benchmark_prices[i]) / benchmark_prices[i]
                for i in range(len(benchmark_prices) - 1)
                if benchmark_prices[i] > 0
            ]
            n = min(len(rets), len(bm_rets))
            if n >= 2:
                mr = sum(rets[:n]) / n
                mb = sum(bm_rets[:n]) / n
                cov = sum((rets[i] - mr) * (bm_rets[i] - mb) for i in range(n)) / (n - 1)
                var_b = sum((bm_rets[i] - mb) ** 2 for i in range(n)) / (n - 1)
                m.beta = cov / var_b if var_b > 0 else 0.0

    # 可靠性
    if m.round_trips < MIN_TRADES_FOR_RELIABILITY:
        m.reliable = False
        m.warnings.append(
            f"仅有 {m.round_trips} 笔完整买卖回合, 少于 {MIN_TRADES_FOR_RELIABILITY} 笔。"
            f"样本量不足, 当前指标(尤其胜率/盈亏比)不具统计意义, 请勿据此判断策略有效性。"
        )
    else:
        m.reliable = True

    if m.trading_days < 20:
        m.warnings.append(f"仅 {m.trading_days} 个交易日, 年化收益波动大, 参考价值有限。")

    return m
