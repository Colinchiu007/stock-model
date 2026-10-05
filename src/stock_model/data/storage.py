"""
数据存储模块

支持:
  - CSV 本地存储
  - Parquet 高效存储
  - 缓存管理 (含TTL过期机制)
  - 内存缓存层 (LRU)
  - 缓存统计 (命中率/命中次数/未命中次数)
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pandas as pd
from loguru import logger

from stock_model.config.settings import get_settings

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class CacheStats:
    """缓存统计信息

    Attributes:
        hits: 缓存命中次数
        misses: 缓存未命中次数
        evictions: 缓存驱逐次数(内存层)
        memory_size: 内存缓存条目数
        disk_size: 磁盘缓存文件数
        disk_bytes: 磁盘缓存总字节数
    """

    hits: int = 0
    misses: int = 0
    evictions: int = 0
    memory_size: int = 0
    disk_size: int = 0
    disk_bytes: int = 0

    @property
    def hit_rate(self) -> float:
        """缓存命中率"""
        total = self.hits + self.misses
        return self.hits / total if total > 0 else 0.0

    @property
    def total_requests(self) -> int:
        """总请求次数"""
        return self.hits + self.misses

    def __str__(self) -> str:
        return (
            f"CacheStats(hits={self.hits}, misses={self.misses}, "
            f"hit_rate={self.hit_rate:.1%}, memory={self.memory_size}, "
            f"disk={self.disk_size}, disk_bytes={self.disk_bytes})"
        )

    def summary(self) -> dict:
        """返回统计摘要字典"""
        return {
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": self.hit_rate,
            "total_requests": self.total_requests,
            "evictions": self.evictions,
            "memory_size": self.memory_size,
            "disk_size": self.disk_size,
            "disk_bytes": self.disk_bytes,
        }


class DataStorage:
    """数据存储管理器

    支持磁盘缓存(Parquet) + 内存缓存层(LRU) + 缓存统计。
    """

    # 默认内存缓存最大条目数
    DEFAULT_MEMORY_CACHE_SIZE = 64

    def __init__(
        self,
        memory_cache_size: int | None = None,
    ):
        self.settings = get_settings()
        self.raw_dir = self.settings.data.raw_data_dir
        self.processed_dir = self.settings.data.processed_data_dir
        self.cache_dir = self.settings.data.cache_dir

        # 内存缓存层 (LRU)
        max_size = memory_cache_size or self.DEFAULT_MEMORY_CACHE_SIZE
        self._memory_cache: OrderedDict[str, pd.DataFrame] = OrderedDict()
        self._memory_cache_max_size = max_size
        self._memory_lock = threading.Lock()

        # 缓存统计
        self._stats = CacheStats()

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
        """从缓存获取数据(先查内存，再查磁盘)"""
        # 1. 查内存缓存
        with self._memory_lock:
            if key in self._memory_cache:
                self._memory_cache.move_to_end(key)  # LRU更新
                self._stats.hits += 1
                logger.debug(f"内存缓存命中: {key}")
                return self._memory_cache[key]

        # 2. 查磁盘缓存
        filepath = self.cache_dir / f"{key}.parquet"
        df = self._load(filepath, "parquet")
        if df is not None:
            self._stats.hits += 1
            # 写入内存缓存
            self._put_memory(key, df)
            return df

        self._stats.misses += 1
        return None

    def cache_set(self, key: str, df: pd.DataFrame) -> None:
        """设置缓存数据(同时写入内存和磁盘)"""
        # 写入内存缓存
        self._put_memory(key, df)

        # 写入磁盘缓存
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        filepath = self.cache_dir / f"{key}.parquet"
        self._save(df, filepath, "parquet")

    def cache_clear(self) -> None:
        """清除所有缓存(内存+磁盘)"""
        with self._memory_lock:
            self._memory_cache.clear()
        for f in self.cache_dir.glob("*.parquet"):
            f.unlink()
        logger.info("缓存已清除(内存+磁盘)")

    def cache_get_with_ttl(self, key: str, ttl_seconds: int = 3600) -> pd.DataFrame | None:
        """从缓存获取数据，支持TTL过期检查

        Args:
            key: 缓存键
            ttl_seconds: 缓存有效期(秒)，默认1小时

        Returns:
            缓存数据(未过期)或None(不存在/已过期)
        """
        # 1. 查内存缓存(内存缓存无TTL，依赖磁盘TTL)
        with self._memory_lock:
            if key in self._memory_cache:
                # 验证磁盘缓存是否过期
                filepath = self.cache_dir / f"{key}.parquet"
                if filepath.exists():
                    file_age = time.time() - filepath.stat().st_mtime
                    if file_age <= ttl_seconds:
                        self._memory_cache.move_to_end(key)
                        self._stats.hits += 1
                        logger.debug(f"内存缓存命中(TTL): {key}")
                        return self._memory_cache[key]
                    # 内存缓存过期，移除
                    del self._memory_cache[key]
                    logger.debug(f"内存缓存过期: {key}")

        # 2. 查磁盘缓存
        filepath = self.cache_dir / f"{key}.parquet"
        if not filepath.exists():
            self._stats.misses += 1
            return None

        # 检查TTL
        file_age = time.time() - filepath.stat().st_mtime
        if file_age > ttl_seconds:
            logger.debug(f"缓存已过期: {key} (年龄={file_age:.0f}s, TTL={ttl_seconds}s)")
            self._stats.misses += 1
            return None

        df = self._load(filepath, "parquet")
        if df is not None:
            self._stats.hits += 1
            self._put_memory(key, df)
            logger.debug(f"磁盘缓存命中: {key} (年龄={file_age:.0f}s)")
        else:
            self._stats.misses += 1
        return df

    def cache_info(self, key: str) -> dict | None:
        """获取缓存信息

        Returns:
            {"exists": bool, "in_memory": bool, "age_seconds": float, "size_bytes": int} 或 None
        """
        in_memory = key in self._memory_cache
        filepath = self.cache_dir / f"{key}.parquet"
        if not filepath.exists() and not in_memory:
            return {"exists": False, "in_memory": False}

        result: dict = {"exists": True, "in_memory": in_memory}
        if filepath.exists():
            stat = filepath.stat()
            result["age_seconds"] = time.time() - stat.st_mtime
            result["size_bytes"] = stat.st_size
        return result

    def cache_stats(self) -> CacheStats:
        """获取缓存统计信息"""
        stats = CacheStats(
            hits=self._stats.hits,
            misses=self._stats.misses,
            evictions=self._stats.evictions,
        )
        with self._memory_lock:
            stats.memory_size = len(self._memory_cache)

        # 磁盘缓存统计
        if self.cache_dir.exists():
            parquet_files = list(self.cache_dir.glob("*.parquet"))
            stats.disk_size = len(parquet_files)
            stats.disk_bytes = sum(f.stat().st_size for f in parquet_files if f.is_file())

        return stats

    def cache_reset_stats(self) -> None:
        """重置缓存统计"""
        self._stats = CacheStats()
        logger.debug("缓存统计已重置")

    def cache_evict_expired(self, ttl_seconds: int = 3600) -> int:
        """驱逐过期的磁盘缓存

        Args:
            ttl_seconds: 过期阈值(秒)

        Returns:
            驱逐的缓存条目数
        """
        if not self.cache_dir.exists():
            return 0

        evicted = 0
        now = time.time()
        for f in self.cache_dir.glob("*.parquet"):
            if now - f.stat().st_mtime > ttl_seconds:
                # 同步移除内存缓存
                key = f.stem
                with self._memory_lock:
                    self._memory_cache.pop(key, None)
                f.unlink()
                evicted += 1

        if evicted > 0:
            logger.info(f"驱逐过期缓存: {evicted}条")
        return evicted

    def _put_memory(self, key: str, df: pd.DataFrame) -> None:
        """写入内存缓存(LRU驱逐)"""
        with self._memory_lock:
            if key in self._memory_cache:
                self._memory_cache.move_to_end(key)
                self._memory_cache[key] = df
            else:
                self._memory_cache[key] = df
                # LRU驱逐
                while len(self._memory_cache) > self._memory_cache_max_size:
                    evicted_key, _ = self._memory_cache.popitem(last=False)
                    self._stats.evictions += 1
                    logger.debug(f"内存缓存LRU驱逐: {evicted_key}")

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
            if format == "csv":
                return pd.read_csv(filepath, index_col=0, parse_dates=True)
            raise ValueError(f"不支持的格式: {format}")
        except (FileNotFoundError, ValueError, OSError) as e:
            logger.error(f"加载数据失败 {filepath}: {e}")
            return None
