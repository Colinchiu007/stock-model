"""
风险管理器

提供组合风险检查、止损止盈、集中度控制等功能。
"""

from __future__ import annotations

from typing import Dict, List, Optional

from loguru import logger

from stock_model.risk.models import (
    PortfolioRisk,
    Position,
    RiskAlert,
    RiskLevel,
    RiskType,
)


class RiskManager:
    """风险管理器

    检查组合和单仓风险，生成风险警报。

    使用示例:
        rm = RiskManager(max_drawdown_limit=0.15)
        alerts = rm.check_portfolio_risk(portfolio)
        for alert in alerts:
            print(alert)
    """

    def __init__(
        self,
        max_drawdown_limit: float = 0.15,
        position_concentration_limit: float = 0.20,
        sector_concentration_limit: float = 0.40,
        default_stop_loss_pct: float = 0.08,
        default_take_profit_pct: float = 0.20,
        volatility_warning_threshold: float = 0.03,
    ):
        self.max_drawdown_limit = max_drawdown_limit
        self.position_concentration_limit = position_concentration_limit
        self.sector_concentration_limit = sector_concentration_limit
        self.default_stop_loss_pct = default_stop_loss_pct
        self.default_take_profit_pct = default_take_profit_pct
        self.volatility_warning_threshold = volatility_warning_threshold

    def check_portfolio_risk(
        self,
        positions: List[Position],
        total_value: float,
        current_drawdown: float = 0.0,
    ) -> List[RiskAlert]:
        """检查组合风险

        Args:
            positions: 持仓列表
            total_value: 组合总市值
            current_drawdown: 当前回撤比例(0-1)

        Returns:
            风险警报列表
        """
        alerts: List[RiskAlert] = []

        # 1. 回撤检查
        if current_drawdown > 0:
            alerts.extend(self._check_drawdown(current_drawdown))

        # 2. 集中度检查
        if total_value > 0:
            alerts.extend(self._check_concentration(positions, total_value))

        # 3. 单仓风险检查
        for position in positions:
            if position.shares > 0:
                alerts.extend(self.check_position_risk(position))

        # 按风险等级排序
        level_order = {
            RiskLevel.CRITICAL: 0,
            RiskLevel.DANGER: 1,
            RiskLevel.WARNING: 2,
            RiskLevel.LOW: 3,
        }
        alerts.sort(key=lambda a: level_order.get(a.level, 99))

        if alerts:
            critical = sum(1 for a in alerts if a.level == RiskLevel.CRITICAL)
            danger = sum(1 for a in alerts if a.level == RiskLevel.DANGER)
            warning = sum(1 for a in alerts if a.level == RiskLevel.WARNING)
            logger.info(
                f"组合风险检查: 严重={critical}, 危险={danger}, 警告={warning}"
            )

        return alerts

    def check_position_risk(self, position: Position) -> List[RiskAlert]:
        """检查单仓风险

        Args:
            position: 持仓信息

        Returns:
            风险警报列表
        """
        alerts: List[RiskAlert] = []

        if position.shares <= 0:
            return alerts

        # 1. 止损检查
        stop_alert = self.check_stop_loss(position)
        if stop_alert:
            alerts.append(stop_alert)

        # 2. 止盈检查
        profit_alert = self.check_take_profit(position)
        if profit_alert:
            alerts.append(profit_alert)

        return alerts

    def check_stop_loss(self, position: Position) -> Optional[RiskAlert]:
        """检查止损

        Args:
            position: 持仓信息

        Returns:
            止损警报(如触发)
        """
        if position.shares <= 0:
            return None

        # 确定止损价
        stop_price = position.stop_loss
        if stop_price is None:
            stop_price = position.cost_price * (1 - self.default_stop_loss_pct)

        if position.current_price <= stop_price:
            return RiskAlert(
                level=RiskLevel.CRITICAL,
                type=RiskType.STOP_LOSS,
                message=f"{position.symbol} 触发止损: 现价={position.current_price:.2f}, 止损价={stop_price:.2f}",
                action="立即卖出",
                symbol=position.symbol,
                value=position.current_price,
                threshold=stop_price,
            )

        # 接近止损(距离5%以内)
        distance = (position.current_price - stop_price) / position.current_price
        if distance < 0.05:
            return RiskAlert(
                level=RiskLevel.WARNING,
                type=RiskType.STOP_LOSS,
                message=f"{position.symbol} 接近止损: 距离={distance:.2%}",
                action="密切关注",
                symbol=position.symbol,
                value=position.current_price,
                threshold=stop_price,
            )

        return None

    def check_take_profit(self, position: Position) -> Optional[RiskAlert]:
        """检查止盈

        Args:
            position: 持仓信息

        Returns:
            止盈警报(如触发)
        """
        if position.shares <= 0:
            return None

        # 确定止盈价
        profit_price = position.take_profit
        if profit_price is None:
            profit_price = position.cost_price * (1 + self.default_take_profit_pct)

        if position.current_price >= profit_price:
            return RiskAlert(
                level=RiskLevel.WARNING,
                type=RiskType.TAKE_PROFIT,
                message=f"{position.symbol} 触发止盈: 现价={position.current_price:.2f}, 止盈价={profit_price:.2f}",
                action="考虑卖出",
                symbol=position.symbol,
                value=position.current_price,
                threshold=profit_price,
            )

        return None

    def _check_drawdown(self, current_drawdown: float) -> List[RiskAlert]:
        """检查回撤风险"""
        alerts = []

        if current_drawdown >= self.max_drawdown_limit:
            alerts.append(
                RiskAlert(
                    level=RiskLevel.CRITICAL,
                    type=RiskType.DRAWDOWN,
                    message=f"回撤超限: {current_drawdown:.2%} > 限制{self.max_drawdown_limit:.2%}",
                    action="减仓或清仓",
                    value=current_drawdown,
                    threshold=self.max_drawdown_limit,
                )
            )
        elif current_drawdown >= self.max_drawdown_limit * 0.8:
            alerts.append(
                RiskAlert(
                    level=RiskLevel.DANGER,
                    type=RiskType.DRAWDOWN,
                    message=f"回撤接近限制: {current_drawdown:.2%}",
                    action="考虑减仓",
                    value=current_drawdown,
                    threshold=self.max_drawdown_limit,
                )
            )
        elif current_drawdown >= self.max_drawdown_limit * 0.5:
            alerts.append(
                RiskAlert(
                    level=RiskLevel.WARNING,
                    type=RiskType.DRAWDOWN,
                    message=f"回撤警告: {current_drawdown:.2%}",
                    action="密切关注",
                    value=current_drawdown,
                    threshold=self.max_drawdown_limit,
                )
            )

        return alerts

    def _check_concentration(
        self, positions: List[Position], total_value: float
    ) -> List[RiskAlert]:
        """检查集中度风险"""
        alerts = []

        for pos in positions:
            if pos.shares <= 0:
                continue

            concentration = pos.market_value / total_value

            if concentration > self.position_concentration_limit:
                level = (
                    RiskLevel.CRITICAL
                    if concentration > self.position_concentration_limit * 1.5
                    else RiskLevel.DANGER
                )
                alerts.append(
                    RiskAlert(
                        level=level,
                        type=RiskType.CONCENTRATION,
                        message=f"{pos.symbol} 集中度={concentration:.2%} > 限制{self.position_concentration_limit:.2%}",
                        action="减仓",
                        symbol=pos.symbol,
                        value=concentration,
                        threshold=self.position_concentration_limit,
                    )
                )

        return alerts