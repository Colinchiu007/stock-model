"""
推送通道

提供控制台、文件、Webhook三种推送通道。
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path

from loguru import logger

from stock_model.strategy.base import StrategyResult


class NotificationChannel(ABC):
    """推送通道基类"""

    @abstractmethod
    def send(self, message: str, result: StrategyResult) -> None:
        """发送通知"""
        ...


class ConsoleChannel(NotificationChannel):
    """控制台推送通道

    直接打印信号到控制台。
    """

    def send(self, message: str, result: StrategyResult) -> None:
        """打印信号到控制台"""
        print(f"\n{'=' * 40}")
        print(message)
        print(f"{'=' * 40}\n")


class FileChannel(NotificationChannel):
    """文件推送通道

    将信号写入JSON文件，每条信号追加一行。
    """

    def __init__(self, filepath: str = "signals.json"):
        self.filepath = Path(filepath)
        self.filepath.parent.mkdir(parents=True, exist_ok=True)

    def send(self, message: str, result: StrategyResult) -> None:
        """写入信号到文件"""
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

        logger.debug(f"信号已写入 {self.filepath}")


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

    def send(self, message: str, result: StrategyResult) -> None:
        """发送信号到Webhook"""
        payload = {
            "msgtype": "text",
            "text": {"content": message},
        }

        try:
            import httpx

            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(self.url, json=payload, headers=self.headers)
                if response.status_code >= 400:
                    logger.warning(
                        f"Webhook返回错误: status={response.status_code}, "
                        f"body={response.text[:200]}"
                    )
                else:
                    logger.debug(f"Webhook发送成功: status={response.status_code}")

        except ImportError:
            logger.warning("httpx 未安装，Webhook推送不可用。请安装: pip install httpx")
        except Exception as e:
            logger.error(f"Webhook发送失败: {e}")
