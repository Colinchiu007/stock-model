"""工具函数测试"""

import numpy as np
import pandas as pd
import pytest

from stock_model.utils.helpers import (
    calculate_max_drawdown,
    calculate_sharpe_ratio,
    calculate_win_rate,
    calculate_profit_factor,
    format_stock_code,
)


class TestFormatStockCode:
    def test_zero_padding(self):
        assert format_stock_code("1") == "000001"

    def test_already_six_digits(self):
        assert format_stock_code("600000") == "600000"

    def test_remove_prefix_sh(self):
        assert format_stock_code("SH600000") == "600000"

    def test_remove_prefix_sz(self):
        assert format_stock_code("sz000001") == "000001"


class TestSharpeRatio:
    def test_positive_returns(self):
        returns = pd.Series([0.01, 0.02, -0.01, 0.03, 0.01])
        sharpe = calculate_sharpe_ratio(returns)
        assert sharpe > 0

    def test_zero_returns(self):
        returns = pd.Series([0.0, 0.0, 0.0])
        sharpe = calculate_sharpe_ratio(returns)
        assert sharpe == 0.0

    def test_empty_returns(self):
        sharpe = calculate_sharpe_ratio(pd.Series([]))
        assert sharpe == 0.0


class TestMaxDrawdown:
    def test_no_drawdown(self):
        prices = pd.Series([1, 2, 3, 4, 5])
        dd = calculate_max_drawdown(prices)
        assert dd == 0.0

    def test_with_drawdown(self):
        prices = pd.Series([1, 2, 1, 3])
        dd = calculate_max_drawdown(prices)
        assert dd < 0
        assert abs(dd - (-0.5)) < 0.01  # 从2到1, 回撤50%


class TestWinRate:
    def test_all_wins(self):
        assert calculate_win_rate([0.01, 0.02, 0.03]) == 1.0

    def test_all_losses(self):
        assert calculate_win_rate([-0.01, -0.02]) == 0.0

    def test_mixed(self):
        assert calculate_win_rate([0.01, -0.01, 0.02]) == pytest.approx(2 / 3)

    def test_empty(self):
        assert calculate_win_rate([]) == 0.0


class TestProfitFactor:
    def test_basic(self):
        pf = calculate_profit_factor([0.01, -0.01, 0.02, -0.01])
        assert pf == pytest.approx(1.5)  # 0.03 / 0.02

    def test_no_losses(self):
        pf = calculate_profit_factor([0.01, 0.02])
        assert pf > 0