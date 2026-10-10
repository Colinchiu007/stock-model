"""
推送通道

提供控制台、文件、Webhook三种推送通道。
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from stock_model.strategy.base import StrategyResult


class NotificationChannel(ABC):
    """推送通道基类

    ``result`` 是**可选**的：
      - 交易信号通知带 ``StrategyResult``（``SignalNotifier.notify``）
      - 系统级告警没有信号对象，传 ``None``（``SignalNotifier.notify_message``）

    这样就不必为"定时任务挂了"另起一套传输 —— Webhook / Console 本来就只用 message。
    """

    @abstractmethod
    def send(self, message: str, result: StrategyResult | None = None) -> None:
        """发送通知"""
        ...


class ConsoleChannel(NotificationChannel):
    """控制台推送通道

    直接打印信号到控制台。
    """

    def send(self, message: str, result: StrategyResult | None = None) -> None:
        """打印信号到控制台

        只用 ``message`` —— 告警(``result=None``)与信号走同一条路。
        """
        print(f"\n{'=' * 40}")
        print(message)
        print(f"{'=' * 40}\n")


class FileChannel(NotificationChannel):
    """文件推送通道

    将信号写入JSON文件，每条信号追加一行。
    传 ``result=None`` 时(系统告警)写一条消息记录, 字段不同但仍是 JSON 行,
    便于用同一个文件按时间顺序回看"信号 + 告警"。
    """

    def __init__(self, filepath: str = "signals.json"):
        self.filepath = Path(filepath)
        self.filepath.parent.mkdir(parents=True, exist_ok=True)

    def send(self, message: str, result: StrategyResult | None = None) -> None:
        """写入通知到文件"""
        # 显式注解: 两个分支的字典键值类型不同(告警是 str→str, 信号含 float),
        # 不注解会让 mypy 拿第一个分支的类型去要求第二个分支(dict-item 报错)
        record: dict[str, Any]
        if result is None:
            record = {
                "timestamp": datetime.now().isoformat(),
                "level": "alert",
                "message": message,
            }
        else:
            record = {
                "timestamp": datetime.now().isoformat(),
                "symbol": result.symbol,
                "action": result.action.value,
                "confidence": result.confidence,
                "reason": result.reason,
                "target_price": result.target_price,
                "stop_loss": result.stop_loss,
                "position_pct": result.position_pct,
            }

        # 追加写入JSON行
        with open(self.filepath, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        logger.debug(f"通知已写入 {self.filepath}")


class WebhookChannel(NotificationChannel):
    """Webhook推送通道

    通过HTTP POST发送信号到指定URL。
    支持钉钉/飞书/企业微信等Webhook。
    依赖 httpx (可选，未安装时记录警告)。
    """

    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: int = 10,
    ):
        self.url = url
        self.headers = headers or {"Content-Type": "application/json"}
        self.timeout = timeout

    @property
    def masked_url(self) -> str:
        """脱敏后的地址(只留主机名) —— 供状态接口回显

        Webhook URL 里通常带 access_token, 属凭据; 直接回显等于把密钥
        打印到日志和界面上。这里只暴露主机名, 足够确认"配没配、配给谁"。
        """
        try:
            from urllib.parse import urlparse

            host = urlparse(self.url).hostname
            return f"{host}/***" if host else "***"
        except ValueError:  # pragma: no cover - urlparse 极少失败
            return "***"

    def send(self, message: str, result: StrategyResult | None = None) -> None:
        """发送消息到Webhook

        只用 ``message``, 故告警(``result=None``)与信号共用本通道。
        """
        payload = {
            "msgtype": "text",
            "text": {"content": message},
        }

        try:
            import httpx
        except ImportError:
            logger.warning("httpx 未安装，Webhook推送不可用。请安装: pip install httpx")
            return

        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(self.url, json=payload, headers=self.headers)
                if response.status_code >= 400:
                    logger.warning(
                        f"Webhook返回错误: status={response.status_code}, "
                        f"body={response.text[:200]}"
                    )
                else:
                    logger.debug(f"Webhook发送成功: status={response.status_code}")

        except (OSError, RuntimeError, TimeoutError, httpx.HTTPError) as e:
            # httpx 的网络类异常(ConnectError / ReadTimeout ...)继承自 HTTPError,
            # **不是** OSError —— 原来只捕 OSError 会让"webhook 不可达"直接穿透给调用方,
            # 于是"通知失败"升级成"定时任务失败"。
            # 本项目对这类漏捕已栽过一次: tenacity.RetryError 也不是 RuntimeError。
            logger.error(f"Webhook发送失败: {e}")
