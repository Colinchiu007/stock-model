"""
信号通知器

管理推送通道，格式化信号消息并分发。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from loguru import logger

from stock_model.config.settings import get_settings
from stock_model.notify.channels import ConsoleChannel, FileChannel, WebhookChannel
from stock_model.strategy.base import ActionType, StrategyResult

if TYPE_CHECKING:
    from stock_model.config.settings import NotifySettings


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
            except (ValueError, OSError, RuntimeError) as e:
                logger.error(f"通知通道 {channel.__class__.__name__} 发送失败: {e}")

    def notify_message(self, message: str) -> None:
        """发送**系统告警**(不是交易信号)

        为什么需要单独一个入口: 通道 ``send`` 的第二个参数是 ``StrategyResult``,
        而"定时任务连续失败 3 次"没有信号对象。传 ``None`` 即可 ——
        Webhook / Console 本来就只用 message, FileChannel 会改写成消息记录。

        与 :meth:`notify` 一致: 单个通道失败只记日志, **绝不影响调用方**。
        这条是刻意的 —— 告警通道挂掉不该把被监控的任务也带崩。
        """
        if not self._channels:
            logger.debug("无通知通道，跳过通知")
            return

        for channel in self._channels:
            try:
                channel.send(message, None)
            except (ValueError, OSError, RuntimeError) as e:
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

    @property
    def description(self) -> str:
        """人类可读的通道描述，**不回显凭据**

        供状态接口回显"到底配了哪些通道"。Webhook 只用 ``masked_url``
        (主机名 + ***), 因为 URL 里通常带 access_token。
        """
        if not self._channels:
            return "(未配置通知通道)"
        parts = []
        for channel in self._channels:
            masked = getattr(channel, "masked_url", None)
            name = channel.__class__.__name__
            parts.append(f"{name}({masked})" if masked else name)
        return " + ".join(parts)


def build_alert_notifier(settings: NotifySettings | None = None) -> SignalNotifier:
    """按配置组装**系统告警**通知器

    通道来源（``STOCK_NOTIFY_*`` 环境变量 / ``.env``）：
      - ``console``     → ConsoleChannel（默认开）
      - ``file_path``   → FileChannel
      - ``webhook_url`` → WebhookChannel（钉钉/飞书/企业微信）

    一个都没配时返回**空通道**的通知器 —— 此时 ``notify_message`` 只记 debug 日志，
    属"用户没配"而非"发送失败"。状态接口会回显 :attr:`SignalNotifier.description`，
    所以"以为配了其实没配"不会隐形。
    """
    cfg = settings or get_settings().notify

    notifier = SignalNotifier()
    if not cfg.enabled:
        logger.info("系统告警已关闭 (STOCK_NOTIFY_ENABLED=false), 不会推送失败提醒")
        return notifier

    if cfg.console:
        notifier.add_channel(ConsoleChannel())
    if cfg.file_path:
        notifier.add_channel(FileChannel(cfg.file_path))
    if cfg.webhook_url:
        notifier.add_channel(WebhookChannel(cfg.webhook_url, timeout=cfg.webhook_timeout))
    return notifier
