"""
工具函数模块

通用辅助函数
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def format_stock_code(symbol: str) -> str:
    """
    格式化股票代码

    Args:
        symbol: 原始股票代码

    Returns:
        6位标准股票代码

    Examples:
        >>> format_stock_code("1")
        '000001'
        >>> format_stock_code("600000")
        '600000'
        >>> format_stock_code("SH600000")
        '600000'
    """
    # 移除前缀
    for prefix in ["SH", "SZ", "sh", "sz"]:
        if symbol.upper().startswith(prefix):
            symbol = symbol[len(prefix) :]

    # 补零到6位
    return symbol.zfill(6)


def calculate_sharpe_ratio(
    returns: pd.Series | np.ndarray,
    risk_free_rate: float = 0.03,
    periods_per_year: int = 252,
) -> float:
    """
    计算夏普比率

    Args:
        returns: 收益率序列
        risk_free_rate: 无风险利率 (年化)
        periods_per_year: 每年交易周期数

    Returns:
        夏普比率
    """
    if isinstance(returns, pd.Series):
        returns = returns.values

    returns = np.asarray(returns, dtype=float)
    returns = returns[~np.isnan(returns)]

    if len(returns) < 2:
        return 0.0

    # 日化无风险利率
    daily_rf = (1 + risk_free_rate) ** (1 / periods_per_year) - 1

    excess_returns = returns - daily_rf
    mean_excess = np.mean(excess_returns)
    std_returns = np.std(returns, ddof=1)

    if std_returns == 0:
        return 0.0

    sharpe = mean_excess / std_returns * np.sqrt(periods_per_year)
    return float(sharpe)


def calculate_max_drawdown(
    prices: pd.Series | np.ndarray,
) -> float:
    """
    计算最大回撤

    Args:
        prices: 价格序列

    Returns:
        最大回撤 (负数)
    """
    if isinstance(prices, pd.Series):
        prices = prices.values

    prices = np.asarray(prices, dtype=float)

    if len(prices) < 2:
        return 0.0

    # 计算累计最高点
    running_max = np.maximum.accumulate(prices)
    # 计算回撤
    drawdowns = (prices - running_max) / running_max
    return float(np.min(drawdowns))


def calculate_win_rate(trades: list[float]) -> float:
    """
    计算胜率

    Args:
        trades: 交易收益率列表

    Returns:
        胜率 (0-1)
    """
    if not trades:
        return 0.0
    wins = sum(1 for t in trades if t > 0)
    return wins / len(trades)


def calculate_profit_factor(trades: list[float]) -> float:
    """
    计算盈亏比

    Args:
        trades: 交易收益率列表

    Returns:
        盈亏比
    """
    profits = [t for t in trades if t > 0]
    losses = [abs(t) for t in trades if t < 0]

    total_profit = sum(profits) if profits else 0
    total_loss = sum(losses) if losses else 1e-10

    return total_profit / total_loss
