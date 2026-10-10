"""
仓位管理器

提供多种仓位计算方法：固定仓位、凯利公式、风险平价、ATR仓位。
"""

from __future__ import annotations

from loguru import logger


class PositionSizer:
    """仓位管理器

    根据不同策略计算建议仓位大小。

    使用示例:
        sizer = PositionSizer()
        shares = sizer.fixed_size(capital=100000, price=10.0)
        shares = sizer.kelly_size(
            capital=100000, price=10.0, win_rate=0.6, avg_win=0.05, avg_loss=0.03
        )
    """

    def __init__(
        self,
        max_position_pct: float = 0.20,
        min_shares: int = 100,
    ):
        self.max_position_pct = max_position_pct  # 单股最大仓位比例
        self.min_shares = min_shares  # 最小交易股数(A股100股)

    def fixed_size(
        self,
        capital: float,
        price: float,
        position_pct: float = 0.10,
    ) -> int:
        """固定比例仓位

        Args:
            capital: 总资金
            price: 股票价格
            position_pct: 仓位比例(默认10%)

        Returns:
            建议买入股数
        """
        position_pct = min(position_pct, self.max_position_pct)
        amount = capital * position_pct
        shares = int(amount / price / self.min_shares) * self.min_shares
        shares = max(0, shares)

        logger.debug(
            f"固定仓位: 资金={capital}, 价格={price}, 比例={position_pct:.0%}, 股数={shares}"
        )
        return shares

    def kelly_size(
        self,
        capital: float,
        price: float,
        win_rate: float,
        avg_win: float,
        avg_loss: float,
        kelly_fraction: float = 0.5,
    ) -> int:
        """凯利公式仓位

        f = (p * b - q) / b
        其中 p=胜率, q=败率, b=盈亏比

        实际使用半凯利(kelly_fraction=0.5)降低风险。

        Args:
            capital: 总资金
            price: 股票价格
            win_rate: 胜率(0-1)
            avg_win: 平均盈利比例
            avg_loss: 平均亏损比例(正数)
            kelly_fraction: 凯利比例(默认0.5=半凯利)

        Returns:
            建议买入股数
        """
        if avg_loss <= 0 or avg_win <= 0:
            logger.warning("凯利公式: 盈亏参数无效，回退到固定仓位")
            return self.fixed_size(capital, price)

        # 盈亏比
        win_loss_ratio = avg_win / avg_loss

        # 凯利公式
        kelly_pct = (win_rate * win_loss_ratio - (1 - win_rate)) / win_loss_ratio

        # 半凯利
        position_pct = kelly_pct * kelly_fraction

        # 限制范围
        position_pct = max(0, min(position_pct, self.max_position_pct))

        if position_pct <= 0:
            logger.info(f"凯利公式建议不买入: 胜率={win_rate:.2f}, 盈亏比={win_loss_ratio:.2f}")
            return 0

        amount = capital * position_pct
        shares = int(amount / price / self.min_shares) * self.min_shares
        shares = max(0, shares)

        logger.debug(
            f"凯利仓位: 胜率={win_rate:.2f}, 盈亏比={win_loss_ratio:.2f}, "
            f"凯利比例={position_pct:.2%}, 股数={shares}"
        )
        return shares

    def risk_parity(
        self,
        capital: float,
        prices: dict[str, float],
        volatilities: dict[str, float],
    ) -> dict[str, int]:
        """风险平价仓位

        按波动率倒数分配仓位，使每只股票风险贡献相等。

        Args:
            capital: 总资金
            prices: 股票价格字典 {symbol: price}
            volatilities: 波动率字典 {symbol: volatility}

        Returns:
            各股票建议股数 {symbol: shares}
        """
        if not prices or not volatilities:
            return {}

        # 波动率倒数作为权重
        inv_vols = {}
        for symbol in prices:
            vol = volatilities.get(symbol, 0.01)
            if vol > 0:
                inv_vols[symbol] = 1.0 / vol
            else:
                inv_vols[symbol] = 0.0

        total_inv_vol = sum(inv_vols.values())
        if total_inv_vol == 0:
            return {}

        # 按权重分配
        result = {}
        for symbol, price in prices.items():
            weight = inv_vols.get(symbol, 0) / total_inv_vol
            amount = capital * weight
            shares = int(amount / price / self.min_shares) * self.min_shares
            result[symbol] = max(0, shares)

        logger.debug(f"风险平价仓位: {result}")
        return result

    def atr_size(
        self,
        capital: float,
        price: float,
        atr: float,
        risk_pct: float = 0.02,
        atr_multiplier: float = 2.0,
    ) -> int:
        """ATR仓位管理

        根据ATR确定止损距离，反推仓位大小。

        Args:
            capital: 总资金
            price: 股票价格
            atr: ATR值(14日)
            risk_pct: 单笔风险占总资金比例(默认2%)
            atr_multiplier: ATR止损倍数(默认2倍)

        Returns:
            建议买入股数
        """
        if atr <= 0 or price <= 0:
            logger.warning("ATR仓位: ATR或价格无效")
            return 0

        # 止损距离
        stop_distance = atr * atr_multiplier

        # 单笔风险金额
        risk_amount = capital * risk_pct

        # 仓位 = 风险金额 / 止损距离
        shares = int(risk_amount / stop_distance / self.min_shares) * self.min_shares

        # 限制最大仓位
        max_amount = capital * self.max_position_pct
        max_shares = int(max_amount / price / self.min_shares) * self.min_shares
        shares = min(shares, max_shares)
        shares = max(0, shares)

        logger.debug(
            f"ATR仓位: ATR={atr:.2f}, 止损距离={stop_distance:.2f}, "
            f"风险={risk_pct:.0%}, 股数={shares}"
        )
        return shares
