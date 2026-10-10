"""信号推送模块

提供交易信号通知功能，支持多种推送通道。
系统级告警（如定时任务失败）复用同一批通道 —— 见 ``notify_message``。
"""

from stock_model.notify.channels import ConsoleChannel, FileChannel, WebhookChannel
from stock_model.notify.notifier import SignalNotifier, build_alert_notifier

__all__ = [
    "ConsoleChannel",
    "FileChannel",
    "SignalNotifier",
    "WebhookChannel",
    "build_alert_notifier",
]
