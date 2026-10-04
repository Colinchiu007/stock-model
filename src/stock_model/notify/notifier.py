"""
信号通知器

管理推送通道，格式化信号消息并分发。
"""

from __future__ import annotations

from datetime import datetime

from loguru import logger

from stock_model.strategy.base import ActionType, StrategyResult


class SignalNotifier:
    """信号通知器

    管理多个推送通道，将交易信号格式化后分发。

    使用示例:
        notifier = SignalNotifier()
        notifier.add_channel(ConsoleChannel())
        notifier.add_channel(FileChannel("signals.json"))

        result = StrategyResult(symbol="000001", action=ActionType.BUY, confidence=0.8)
        notifier.notify(result)
    """

    def __init__(self):
        self._channels = []

    def add_channel(self, channel) -> None:
        """添加推送通道

        Args:
            channel: 通知通道实例(需实现send方法)
        """
        self._channels.append(channel)
        logger.info(f"添加通知通道: {channel.__class__.__name__}")

    def remove_channel(self, channel) -> None:
        """移除推送通道"""
        if channel in self._channels:
            self._channels.remove(channel)

    def notify(self, result: StrategyResult) -> None:
        """发送信号通知

        Args:
            result: 策略执行结果
        """
        if not self._channels:
            logger.debug("无通知通道，跳过通知")
            return

        message = self._format_message(result)

        for channel in self._channels:
            try:
                channel.send(message, result)
            except Exception as e:
                logger.error(f"通知通道 {channel.__class__.__name__} 发送失败: {e}")

    def _format_message(self, result: StrategyResult) -> str:
        """格式化信号消息"""
        action_emoji = {
            ActionType.BUY: "🟢",
            ActionType.SELL: "🔴",
            ActionType.HOLD: "🟡",
        }

        emoji = action_emoji.get(result.action, "⚪")
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        lines = [
            f"{emoji} 交易信号 | {timestamp}",
            f"股票: {result.symbol}",
            f"操作: {result.action.value.upper()}",
            f"信心度: {result.confidence:.1%}",
            f"原因: {result.reason}",
        ]

        if result.target_price:
            lines.append(f"目标价: {result.target_price:.2f}")
        if result.stop_loss:
            lines.append(f"止损价: {result.stop_loss:.2f}")
        if result.position_pct > 0:
            lines.append(f"建议仓位: {result.position_pct:.0f}%")

        return "\n".join(lines)
