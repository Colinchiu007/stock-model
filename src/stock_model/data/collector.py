"""
自动数据采集器

支持定时采集股票数据，监控数据质量。
依赖 apscheduler (可选，未安装时降级为手动触发)。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from loguru import logger

from stock_model.data.fetcher import StockDataFetcher

if TYPE_CHECKING:
    from collections.abc import Callable

    import pandas as pd
    from apscheduler.schedulers.background import BackgroundScheduler


class MissingDependencyError(ImportError):
    """缺少可选依赖时抛出

    继承 ``ImportError`` 而非 ``RuntimeError``：
    历史实现是静默返回, 调用方(包括既有测试)习惯用
    ``except ImportError`` 兜底, 继承它可保持向后兼容。
    语义上它确实表示「某个包没装」, 比 RuntimeError 更准确。
    """


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
        # 回调既可以是 callback(symbol, df), 也可以是 ("error", callback) 二元组
        self._callbacks: list[Callable[..., Any] | tuple[str, Callable[..., Any]]] = []
        # apscheduler 是可选依赖: 未安装时保持 None, start() 会抛 MissingDependencyError。
        # 必须写注解 —— 不写时 mypy 把类型推断成 None, 于是 add_job()/start()
        # 被报成「"None" has no attribute ...」(属注解缺失, 非运行时缺陷)
        self._scheduler: BackgroundScheduler | None = None
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
            s = symbol.strip()
            if s and s not in self._watchlist:
                self._watchlist.append(s)
                logger.info(f"添加监控: {s}")

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
                        # 写 isinstance(cb, tuple) 而不是 `cb[0] == "error"`:
                        # 前者能让类型收窄成立(否则 mypy 报 "tuple[...] not callable"),
                        # 而且未知标签会被显式告警, 而不是被当成函数直接调用后
                        # 冒出一句莫名其妙的 "'tuple' object is not callable"。
                        if isinstance(cb, tuple):
                            if cb[0] != "error":
                                logger.warning(f"未处理的数据回调标签 {cb[0]!r}, 已跳过")
                            continue
                        try:
                            cb(symbol, df)
                        except (ValueError, TypeError) as e:
                            logger.warning(f"数据回调执行失败: {e}")
                else:
                    logger.warning(f"采集 {symbol} 返回空数据")
                    self._error_count += 1

            except (ValueError, KeyError, ConnectionError, RuntimeError) as e:
                logger.error(f"采集 {symbol} 失败: {e}")
                self._error_count += 1

                # 触发错误回调
                for cb in self._callbacks:
                    if isinstance(cb, tuple) and cb[0] == "error":
                        try:
                            cb[1](symbol, e)
                        except (ValueError, TypeError) as cb_err:
                            logger.warning(f"错误回调执行失败: {cb_err}")

        logger.info(f"采集完成: 成功={len(results)}, 失败={len(self._watchlist) - len(results)}")
        return results

    def start(self, interval_minutes: int = 30) -> None:
        """启动定时采集

        Args:
            interval_minutes: 采集间隔(分钟)

        Raises:
            MissingDependencyError: apscheduler 未安装时。

                继承自 ImportError 以保持向后兼容 —— 此前此处在缺依赖时
                静默返回, 既有测试用 `except ImportError` 兜底。
                改为抛出可让调用方(pipeline / web API)感知失败,
                避免"界面显示运行中但实际没跑"的假成功。

        """
        if self._running:
            logger.warning("定时采集已在运行")
            return

        try:
            from apscheduler.schedulers.background import BackgroundScheduler
        except ImportError as err:
            raise MissingDependencyError(
                "定时采集不可用: 未安装 apscheduler。请执行 "
                "`pip install apscheduler` 或 `pip install stock-model[schedule]`；"
                "也可调用 collect_now() 手动触发采集。"
            ) from err

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

    def stop(self) -> None:
        """停止定时采集"""
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

        self._running = False
        logger.info("定时采集已停止")
