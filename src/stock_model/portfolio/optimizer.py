"""
组合优化器

提供均值方差优化、风险平价、最小方差等组合优化方法。
依赖 scipy (可选，未安装时使用简化算法)。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from loguru import logger

from stock_model.portfolio.models import Portfolio, PortfolioWeight


class PortfolioOptimizer:
    """组合优化器

    使用示例:
        optimizer = PortfolioOptimizer()
        portfolio = optimizer.equal_weight(symbols, prices)
        portfolio = optimizer.risk_parity(returns)
        portfolio = optimizer.min_variance(returns)
    """

    def __init__(self, risk_free_rate: float = 0.03):
        self.risk_free_rate = risk_free_rate  # 无风险利率(年化)

    def equal_weight(
        self,
        symbols: list[str],
        prices: dict[str, float] | None = None,
        total_value: float = 100000.0,
    ) -> Portfolio:
        """等权重组合

        Args:
            symbols: 股票代码列表
            prices: 当前价格字典 {symbol: price}
            total_value: 总资金

        Returns:
            等权重组合
        """
        n = len(symbols)
        if n == 0:
            return Portfolio(name="equal_weight")

        weight = 1.0 / n
        weights = []

        for symbol in symbols:
            price = prices.get(symbol, 0.0) if prices else 0.0
            amount = total_value * weight
            shares = int(amount / price / 100) * 100 if price > 0 else 0

            weights.append(
                PortfolioWeight(
                    symbol=symbol,
                    weight=weight,
                    shares=shares,
                    price=price,
                )
            )

        portfolio = Portfolio(
            name="equal_weight",
            total_value=total_value,
            weights=weights,
            metrics={"method": "equal_weight", "n_assets": n},
        )

        logger.info(f"等权重组合: {n}只股票, 每只权重={weight:.2%}")
        return portfolio

    def risk_parity(
        self,
        returns: dict[str, pd.Series] | pd.DataFrame,
        prices: dict[str, float] | None = None,
        total_value: float = 100000.0,
    ) -> Portfolio:
        """风险平价组合

        按波动率倒数分配权重，使每只股票风险贡献相等。

        Args:
            returns: 收益率 Dict{symbol: Series} 或 DataFrame (columns=symbols)
            prices: 当前价格字典
            total_value: 总资金

        Returns:
            风险平价组合
        """
        # 统一转换为DataFrame
        if isinstance(returns, dict):
            returns = pd.DataFrame(returns)
        if returns.empty:
            return Portfolio(name="risk_parity")

        # 计算波动率
        vols = returns.std() * np.sqrt(252)  # 年化波动率
        vols = vols.replace(0, np.nan).dropna()

        if len(vols) == 0:
            logger.warning("风险平价: 所有波动率为0，回退到等权重")
            return self.equal_weight(list(returns.columns), prices, total_value)

        # 波动率倒数权重
        inv_vols = 1.0 / vols
        raw_weights = inv_vols / inv_vols.sum()

        symbols = list(raw_weights.index)
        weights = []

        for symbol in symbols:
            w = float(raw_weights[symbol])
            price = prices.get(symbol, 0.0) if prices else 0.0
            amount = total_value * w
            shares = int(amount / price / 100) * 100 if price > 0 else 0

            weights.append(
                PortfolioWeight(
                    symbol=symbol,
                    weight=w,
                    shares=shares,
                    price=price,
                )
            )

        portfolio = Portfolio(
            name="risk_parity",
            total_value=total_value,
            weights=weights,
            metrics={
                "method": "risk_parity",
                "n_assets": len(symbols),
                "volatilities": {s: float(vols.get(s, 0)) for s in symbols},
            },
        )

        logger.info(f"风险平价组合: {len(symbols)}只股票")
        return portfolio

    def min_variance(
        self,
        returns: dict[str, pd.Series] | pd.DataFrame,
        prices: dict[str, float] | None = None,
        total_value: float = 100000.0,
    ) -> Portfolio:
        """最小方差组合

        使用scipy.optimize求解最小方差组合。
        未安装scipy时使用简化算法。

        Args:
            returns: 收益率 Dict{symbol: Series} 或 DataFrame
            prices: 当前价格字典
            total_value: 总资金

        Returns:
            最小方差组合
        """
        # 统一转换为DataFrame
        if isinstance(returns, dict):
            returns = pd.DataFrame(returns)
        if returns.empty:
            return Portfolio(name="min_variance")

        cov_matrix = returns.cov() * 252  # 年化协方差
        symbols = list(cov_matrix.columns)
        n = len(symbols)

        if n <= 1:
            return self.equal_weight(symbols, prices, total_value)

        try:
            from scipy.optimize import minimize

            # 目标函数: 组合方差
            def portfolio_variance(w):
                return np.dot(w, np.dot(cov_matrix.values, w))

            # 约束: 权重和为1
            constraints = {"type": "eq", "fun": lambda w: np.sum(w) - 1.0}

            # 边界: 权重在0-1之间
            bounds = tuple((0.0, 1.0) for _ in range(n))

            # 初始猜测: 等权重
            x0 = np.array([1.0 / n] * n)

            result = minimize(
                portfolio_variance,
                x0,
                method="SLSQP",
                bounds=bounds,
                constraints=constraints,
                options={"maxiter": 1000},
            )

            if result.success:
                opt_weights = result.x
            else:
                logger.warning(f"最小方差优化失败: {result.message}, 使用等权重")
                opt_weights = np.array([1.0 / n] * n)

        except ImportError:
            logger.warning("scipy 未安装，使用简化最小方差算法")
            # 简化算法: 按方差倒数加权
            variances = np.diag(cov_matrix.values)
            inv_var = 1.0 / np.maximum(variances, 1e-10)
            opt_weights = inv_var / inv_var.sum()

        # 构建组合
        weights = []
        for i, symbol in enumerate(symbols):
            w = float(opt_weights[i])
            price = prices.get(symbol, 0.0) if prices else 0.0
            amount = total_value * w
            shares = int(amount / price / 100) * 100 if price > 0 else 0

            weights.append(
                PortfolioWeight(
                    symbol=symbol,
                    weight=w,
                    shares=shares,
                    price=price,
                )
            )

        # 计算组合方差
        port_var = float(np.dot(opt_weights, np.dot(cov_matrix.values, opt_weights)))
        port_vol = float(np.sqrt(port_var))

        portfolio = Portfolio(
            name="min_variance",
            total_value=total_value,
            weights=weights,
            metrics={
                "method": "min_variance",
                "n_assets": n,
                "portfolio_volatility": port_vol,
            },
        )

        logger.info(f"最小方差组合: {n}只股票, 波动率={port_vol:.2%}")
        return portfolio

    def mean_variance(
        self,
        returns: dict[str, pd.Series] | pd.DataFrame,
        prices: dict[str, float] | None = None,
        total_value: float = 100000.0,
        target_return: float | None = None,
    ) -> Portfolio:
        """均值方差优化(Markowitz)

        在给定目标收益下最小化方差，或最大化夏普比率。

        Args:
            returns: 收益率 Dict{symbol: Series} 或 DataFrame
            prices: 当前价格字典
            total_value: 总资金
            target_return: 目标年化收益率(None则最大化夏普比率)

        Returns:
            最优组合
        """
        # 统一转换为DataFrame
        if isinstance(returns, dict):
            returns = pd.DataFrame(returns)
        if returns.empty:
            return Portfolio(name="mean_variance")

        mean_returns = returns.mean() * 252  # 年化收益
        cov_matrix = returns.cov() * 252  # 年化协方差
        symbols = list(mean_returns.index)
        n = len(symbols)

        if n <= 1:
            return self.equal_weight(symbols, prices, total_value)

        try:
            from scipy.optimize import minimize

            if target_return is not None:
                # 给定目标收益下最小化方差
                def objective(w):
                    return np.dot(w, np.dot(cov_matrix.values, w))

                constraints = [
                    {"type": "eq", "fun": lambda w: np.sum(w) - 1.0},
                    {"type": "eq", "fun": lambda w: np.dot(w, mean_returns.values) - target_return},
                ]
            else:
                # 最大化夏普比率 = 最小化负夏普比率
                def objective(w):
                    port_return = np.dot(w, mean_returns.values)
                    port_vol = np.sqrt(np.dot(w, np.dot(cov_matrix.values, w)))
                    if port_vol == 0:
                        return 0.0
                    sharpe = (port_return - self.risk_free_rate) / port_vol
                    return -sharpe

                constraints = [
                    {"type": "eq", "fun": lambda w: np.sum(w) - 1.0},
                ]

            bounds = tuple((0.0, 1.0) for _ in range(n))
            x0 = np.array([1.0 / n] * n)

            result = minimize(
                objective,
                x0,
                method="SLSQP",
                bounds=bounds,
                constraints=constraints,
                options={"maxiter": 1000},
            )

            if result.success:
                opt_weights = result.x
            else:
                logger.warning(f"均值方差优化失败: {result.message}, 使用等权重")
                opt_weights = np.array([1.0 / n] * n)

        except ImportError:
            logger.warning("scipy 未安装，使用等权重")
            opt_weights = np.array([1.0 / n] * n)

        # 构建组合
        weights = []
        for i, symbol in enumerate(symbols):
            w = float(opt_weights[i])
            price = prices.get(symbol, 0.0) if prices else 0.0
            amount = total_value * w
            shares = int(amount / price / 100) * 100 if price > 0 else 0

            weights.append(
                PortfolioWeight(
                    symbol=symbol,
                    weight=w,
                    shares=shares,
                    price=price,
                )
            )

        # 计算组合指标
        port_return = float(np.dot(opt_weights, mean_returns.values))
        port_var = float(np.dot(opt_weights, np.dot(cov_matrix.values, opt_weights)))
        port_vol = float(np.sqrt(port_var))
        sharpe = (port_return - self.risk_free_rate) / port_vol if port_vol > 0 else 0.0

        portfolio = Portfolio(
            name="mean_variance",
            total_value=total_value,
            weights=weights,
            metrics={
                "method": "mean_variance",
                "n_assets": n,
                "expected_return": port_return,
                "portfolio_volatility": port_vol,
                "sharpe_ratio": sharpe,
            },
        )

        logger.info(
            f"均值方差组合: {n}只股票, "
            f"预期收益={port_return:.2%}, 波动率={port_vol:.2%}, 夏普={sharpe:.2f}"
        )
        return portfolio
