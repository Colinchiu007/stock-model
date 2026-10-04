"""
数据存储模块

支持:
  - CSV 本地存储
  - Parquet 高效存储
  - 缓存管理 (含TTL过期机制)
"""

from __future__ import annotations

import time
from pathlib import Path

import pandas as pd
from loguru import logger

from stock_model.config.settings import get_settings


class DataStorage:
    """数据存储管理器"""

    def __init__(self):
        self.settings = get_settings()
        self.raw_dir = self.settings.data.raw_data_dir
        self.processed_dir = self.settings.data.processed_data_dir
        self.cache_dir = self.settings.data.cache_dir

    # ==================== 保存 ====================

    def save_raw(
        self,
        df: pd.DataFrame,
        symbol: str,
        data_type: str = "daily",
        format: str = "parquet",
    ) -> Path:
        """
        保存原始数据

        Args:
            df: 数据
            symbol: 股票代码
            data_type: 数据类型 daily/weekly/monthly
            format: 存储格式 csv/parquet

        Returns:
            保存的文件路径
        """
        subdir = self.raw_dir / data_type
        subdir.mkdir(parents=True, exist_ok=True)

        filepath = subdir / f"{symbol}.{format}"
        self._save(df, filepath, format)
        logger.info(f"保存原始数据: {filepath}")
        return filepath

    def save_processed(
        self,
        df: pd.DataFrame,
        symbol: str,
        data_type: str = "daily",
        format: str = "parquet",
    ) -> Path:
        """保存处理后数据"""
        subdir = self.processed_dir / data_type
        subdir.mkdir(parents=True, exist_ok=True)

        filepath = subdir / f"{symbol}.{format}"
        self._save(df, filepath, format)
        logger.info(f"保存处理后数据: {filepath}")
        return filepath

    # ==================== 加载 ====================

    def load_raw(
        self,
        symbol: str,
        data_type: str = "daily",
        format: str = "parquet",
    ) -> pd.DataFrame | None:
        """加载原始数据"""
        filepath = self.raw_dir / data_type / f"{symbol}.{format}"
        return self._load(filepath, format)

    def load_processed(
        self,
        symbol: str,
        data_type: str = "daily",
        format: str = "parquet",
    ) -> pd.DataFrame | None:
        """加载处理后数据"""
        filepath = self.processed_dir / data_type / f"{symbol}.{format}"
        return self._load(filepath, format)

    # ==================== 缓存 ====================

    def cache_get(self, key: str) -> pd.DataFrame | None:
        """从缓存获取数据"""
        filepath = self.cache_dir / f"{key}.parquet"
        return self._load(filepath, "parquet")

    def cache_set(self, key: str, df: pd.DataFrame) -> None:
        """设置缓存数据"""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        filepath = self.cache_dir / f"{key}.parquet"
        self._save(df, filepath, "parquet")

    def cache_clear(self) -> None:
        """清除所有缓存"""
        for f in self.cache_dir.glob("*.parquet"):
            f.unlink()
        logger.info("缓存已清除")

    def cache_get_with_ttl(self, key: str, ttl_seconds: int = 3600) -> pd.DataFrame | None:
        """从缓存获取数据，支持TTL过期检查

        Args:
            key: 缓存键
            ttl_seconds: 缓存有效期(秒)，默认1小时

        Returns:
            缓存数据(未过期)或None(不存在/已过期)
        """
        filepath = self.cache_dir / f"{key}.parquet"
        if not filepath.exists():
            return None

        # 检查TTL
        file_age = time.time() - filepath.stat().st_mtime
        if file_age > ttl_seconds:
            logger.debug(f"缓存已过期: {key} (年龄={file_age:.0f}s, TTL={ttl_seconds}s)")
            return None

        df = self._load(filepath, "parquet")
        if df is not None:
            logger.debug(f"缓存命中: {key} (年龄={file_age:.0f}s)")
        return df

    def cache_info(self, key: str) -> dict | None:
        """获取缓存信息

        Returns:
            {"exists": bool, "age_seconds": float, "size_bytes": int} 或 None
        """
        filepath = self.cache_dir / f"{key}.parquet"
        if not filepath.exists():
            return {"exists": False}

        stat = filepath.stat()
        return {
            "exists": True,
            "age_seconds": time.time() - stat.st_mtime,
            "size_bytes": stat.st_size,
        }

    # ==================== 内部方法 ====================

    def _save(self, df: pd.DataFrame, filepath: Path, format: str) -> None:
        """保存数据到文件"""
        filepath.parent.mkdir(parents=True, exist_ok=True)
        if format == "parquet":
            df.to_parquet(filepath, index=True)
        elif format == "csv":
            df.to_csv(filepath, index=True, encoding="utf-8")
        else:
            raise ValueError(f"不支持的格式: {format}")

    def _load(self, filepath: Path, format: str) -> pd.DataFrame | None:
        """从文件加载数据"""
        if not filepath.exists():
            return None
        try:
            if format == "parquet":
                return pd.read_parquet(filepath)
            elif format == "csv":
                return pd.read_csv(filepath, index_col=0, parse_dates=True)
            else:
                raise ValueError(f"不支持的格式: {format}")
        except Exception as e:
            logger.error(f"加载数据失败 {filepath}: {e}")
            return None
