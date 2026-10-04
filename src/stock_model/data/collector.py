"""
自动数据采集器

支持定时采集股票数据，监控数据质量。
依赖 apscheduler (可选，未安装时降级为手动触发)。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import pandas as pd
from loguru import logger

from stock_model.data.fetcher import StockDataFetcher


class AutoDataCollector:
    """自动数据采集器

    支持定时采集和手动触发，采集完成后通过回调通知。

    使用示例:
        collector = AutoDataCollector()
        collector.add_watchlist(["000001", "600000"])
        collector.on_data(lambda symbol, df: print(f"{symbol}: {len(df)}行"))
        collector.collect_now()  # 手动触发
    """

    def __init__(self, fetcher: StockDataFetcher | None = None):
        self._fetcher = fetcher or StockDataFetcher()
        self._watchlist: list[str] = []
        self._callbacks: list[Callable] = []
        self._scheduler = None
        self._running = False
        self._collect_count = 0
        self._error_count = 0
        self._last_collect_time: datetime | None = None

    @property
    def watchlist(self) -> list[str]:
        """当前监控列表"""
        return self._watchlist.copy()

    @property
    def is_running(self) -> bool:
        """是否正在定时采集"""
        return self._running

    @property
    def stats(self) -> dict[str, object]:
        """采集统计"""
        return {
            "collect_count": self._collect_count,
            "error_count": self._error_count,
            "last_collect_time": self._last_collect_time,
            "watchlist_size": len(self._watchlist),
        }

    def add_watchlist(self, symbols: list[str]) -> None:
        """添加监控股票

        Args:
            symbols: 股票代码列表
        """
        for symbol in symbols:
            symbol = symbol.strip()
            if symbol and symbol not in self._watchlist:
                self._watchlist.append(symbol)
                logger.info(f"添加监控: {symbol}")

    def remove_watchlist(self, symbols: list[str]) -> None:
        """移除监控股票"""
        for symbol in symbols:
            if symbol in self._watchlist:
                self._watchlist.remove(symbol)
                logger.info(f"移除监控: {symbol}")

    def on_data(self, callback: Callable[[str, pd.DataFrame], None]) -> None:
        """注册数据到达回调

        Args:
            callback: 回调函数，签名 callback(symbol: str, df: pd.DataFrame)
        """
        self._callbacks.append(callback)

    def on_error(self, callback: Callable[[str, Exception], None]) -> None:
        """注册错误回调

        Args:
            callback: 回调函数，签名 callback(symbol: str, error: Exception)
        """
        self._callbacks.append(("error", callback))

    def collect_now(self) -> dict[str, pd.DataFrame]:
        """立即采集所有监控股票数据

        Returns:
            各股票数据字典 {symbol: DataFrame}
        """
        logger.info(f"开始采集 {len(self._watchlist)} 只股票数据")
        results: dict[str, pd.DataFrame] = {}
        self._last_collect_time = datetime.now()

        for symbol in self._watchlist:
            try:
                df = self._fetcher.get_daily(symbol)
                if df is not None and not df.empty:
                    results[symbol] = df
                    self._collect_count += 1

                    # 触发数据回调
                    for cb in self._callbacks:
                        if isinstance(cb, tuple) and cb[0] == "error":
                            continue
                        try:
                            cb(symbol, df)
                        except Exception as e:
                            logger.warning(f"数据回调执行失败: {e}")
                else:
                    logger.warning(f"采集 {symbol} 返回空数据")
                    self._error_count += 1

            except Exception as e:
                logger.error(f"采集 {symbol} 失败: {e}")
                self._error_count += 1

                # 触发错误回调
                for cb in self._callbacks:
                    if isinstance(cb, tuple) and cb[0] == "error":
                        try:
                            cb[1](symbol, e)
                        except Exception as cb_err:
                            logger.warning(f"错误回调执行失败: {cb_err}")

        logger.info(f"采集完成: 成功={len(results)}, 失败={len(self._watchlist) - len(results)}")
        return results

    def start(self, interval_minutes: int = 30) -> None:
        """启动定时采集

        Args:
            interval_minutes: 采集间隔(分钟)
        """
        if self._running:
            logger.warning("定时采集已在运行")
            return

        try:
            from apscheduler.schedulers.background import BackgroundScheduler

            self._scheduler = BackgroundScheduler()
            self._scheduler.add_job(
                self.collect_now,
                "interval",
                minutes=interval_minutes,
                id="stock_data_collect",
            )
            self._scheduler.start()
            self._running = True
            logger.info(f"定时采集已启动，间隔={interval_minutes}分钟")

        except ImportError:
            logger.warning("apscheduler 未安装，定时采集不可用。请安装: pip install apscheduler")
            logger.info("可使用 collect_now() 手动触发采集")

    def stop(self) -> None:
        """停止定时采集"""
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

        self._running = False
        logger.info("定时采集已停止")
