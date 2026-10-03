"""
日志配置模块

使用 loguru 统一管理日志
"""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

from stock_model.config.settings import get_settings


def setup_logger(level: str | None = None) -> None:
    """
    配置日志

    Args:
        level: 日志级别，默认从配置读取
    """
    settings = get_settings()
    log_level = level or settings.log_level
    log_dir = settings.log_dir

    # 移除默认handler
    logger.remove()

    # 控制台输出
    logger.add(
        sys.stderr,
        level=log_level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
        "<level>{message}</level>",
        colorize=True,
    )

    # 文件输出
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.add(
        str(log_dir / "stock_model_{time:YYYY-MM-DD}.log"),
        level=log_level,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} | {message}",
        rotation="1 day",
        retention="30 days",
        compression="zip",
        encoding="utf-8",
    )

    logger.info(f"日志系统初始化完成, 级别: {log_level}")