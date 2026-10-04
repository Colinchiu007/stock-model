"""
股票数据获取模块

支持多种数据源:
  - akshare: 免费A股数据 (默认, 基于东方财富)
  - baostock: 免费A股数据 (稳定备选, 基于证券宝, 不受反爬影响)
  - tushare: 需要Token (专业级, 预留接口)

功能:
  - 日线/周线/月线行情
  - 实时行情
  - 估值数据 (PE/PB/PS/ROE等)
  - 财务摘要
  - 板块数据
  - 自动降级: akshare失败时自动切换到baostock
  - TTL缓存: 减少重复网络请求
"""

from __future__ import annotations

import os

import pandas as pd
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from stock_model.config.settings import get_settings
from stock_model.data.sources.akshare_source import AkshareSource
from stock_model.data.sources.baostock_source import BaostockSource
from stock_model.data.sources.base import DataSource
from stock_model.data.storage import DataStorage

# akshare/efinance 常见的连接错误模式，触发自动降级
_CONNECTION_ERROR_PATTERNS = (
    "ConnectionError",
    "RemoteDisconnected",
    "Connection aborted",
    "Connection reset",
    "Remote end closed connection",
    "Max retries exceeded",
    "NewConnectionError",
    "SSLError",
)


class StockDataFetcher:
    """股票数据获取器

    支持多数据源和自动降级:
      - 默认使用akshare获取数据
      - 当akshare因东方财富反爬策略失败时，自动降级到baostock
      - 可通过配置指定主数据源和备选数据源

    使用示例:
        fetcher = StockDataFetcher()  # 默认akshare + 自动降级
        fetcher = StockDataFetcher(source="baostock")  # 直接使用baostock
        df = fetcher.get_daily("000001", start_date="20240101")
    """

    # 可用的数据源名称
    AVAILABLE_SOURCES = ("akshare", "baostock")

    def __init__(self, source: str | None = None):
        self.settings = get_settings()
        self.source = source or self.settings.data.default_source
        self._setup_proxy()
        self._source_instance: DataSource | None = None
        self._fallback_instance: DataSource | None = None
        self._init_sources()
        # 缓存
        self._storage = DataStorage()
        self._cache_enabled = self.settings.data.cache_enabled
        self._cache_ttl = self.settings.data.cache_ttl
        logger.info(
            f"数据获取器初始化, 主数据源: {self.source}, "
            f"自动降级: 已启用, 缓存: {'启用' if self._cache_enabled else '禁用'}"
        )

    def _init_sources(self) -> None:
        """初始化数据源实例"""
        self._source_instance = self._create_source(self.source)

        # 自动降级: 如果主数据源是akshare，备选为baostock
        if self.source == "akshare":
            try:
                self._fallback_instance = BaostockSource()
                logger.info("备选数据源: baostock (自动降级已启用)")
            except Exception as e:
                logger.warning(f"baostock备选数据源初始化失败: {e}")
                self._fallback_instance = None
        elif self.source == "baostock":
            self._fallback_instance = None
            logger.info("baostock作为主数据源，无备选")

    @staticmethod
    def _create_source(name: str) -> DataSource:
        """根据名称创建数据源实例"""
        if name == "akshare":
            return AkshareSource()
        elif name == "baostock":
            return BaostockSource()
        else:
            raise ValueError(f"不支持的数据源: {name}, 可选: {StockDataFetcher.AVAILABLE_SOURCES}")

    def _is_connection_error(self, error: Exception) -> bool:
        """判断是否为连接类错误（可降级）

        同时检查错误消息和异常类名，确保ConnectionError等异常也能被正确识别。
        """
        error_str = str(error)
        class_name = error.__class__.__name__
        return any(
            pattern in error_str or pattern in class_name for pattern in _CONNECTION_ERROR_PATTERNS
        )

    def _execute_with_fallback(self, method_name: str, *args, **kwargs) -> pd.DataFrame:
        """执行数据获取方法，失败时自动降级到备选数据源

        Args:
            method_name: 数据源方法名 (如 "get_daily")
            *args, **kwargs: 传递给数据源方法的参数

        Returns:
            DataFrame

        Raises:
            RuntimeError: 主数据源和备选数据源都失败时
        """
        # 尝试主数据源
        try:
            method = getattr(self._source_instance, method_name)
            result = method(*args, **kwargs)
            if result is not None and not result.empty:
                return result
            # 空数据也尝试降级
            logger.warning(f"[{self.source}] {method_name} 返回空数据，尝试备选数据源")
        except Exception as e:
            if self._is_connection_error(e) and self._fallback_instance is not None:
                logger.warning(
                    f"[{self.source}] {method_name} 连接失败: {e.__class__.__name__}: {e}，"
                    f"自动降级到 {self._fallback_instance.name}"
                )
            else:
                # 非连接错误，不降级，直接抛出
                raise

        # 尝试备选数据源
        if self._fallback_instance is not None:
            try:
                method = getattr(self._fallback_instance, method_name)
                result = method(*args, **kwargs)
                if result is not None and not result.empty:
                    logger.info(f"[{self._fallback_instance.name}] {method_name} 降级获取成功")
                    return result
                logger.warning(f"[{self._fallback_instance.name}] {method_name} 也返回空数据")
            except Exception as fallback_error:
                logger.error(
                    f"[{self._fallback_instance.name}] {method_name} 也失败: "
                    f"{fallback_error.__class__.__name__}: {fallback_error}"
                )

        raise RuntimeError(f"数据获取失败: 主数据源[{self.source}]和备选数据源均无法获取数据")

    def _setup_proxy(self) -> None:
        """根据配置设置HTTP代理

        当proxy_url为"none"时，会强制绕过系统代理(包括Windows注册表中的代理设置)，
        通过monkey-patch requests.Session使trust_env=False来实现。
        """
        proxy_url = (self.settings.data.proxy_url or "").strip()
        no_proxy = (self.settings.data.no_proxy or "").strip()

        if proxy_url and proxy_url.lower() != "none":
            os.environ["HTTP_PROXY"] = proxy_url
            os.environ["HTTPS_PROXY"] = proxy_url
            logger.info(f"已设置HTTP代理: {proxy_url}")
        elif proxy_url and proxy_url.lower() == "none":
            # 显式禁用代理: 清除环境变量 + 绕过系统代理
            os.environ.pop("HTTP_PROXY", None)
            os.environ.pop("HTTPS_PROXY", None)
            os.environ.pop("http_proxy", None)
            os.environ.pop("https_proxy", None)
            # 设置NO_PROXY=* 绕过所有代理
            os.environ["NO_PROXY"] = "*"
            os.environ["no_proxy"] = "*"
            # Monkey-patch requests.Session 使trust_env=False
            # 这样即使Windows注册表有系统代理，requests也会忽略
            self._patch_requests_trust_env()
            logger.info("已禁用HTTP代理(含系统代理)")

        if no_proxy and proxy_url.lower() != "none":
            os.environ["NO_PROXY"] = no_proxy
            os.environ["no_proxy"] = no_proxy

    @staticmethod
    def _patch_requests_trust_env() -> None:
        """Monkey-patch requests.Session 默认trust_env=False

        使requests忽略系统代理设置(如Windows注册表中的代理)。
        这对akshare等直接调用requests.get()的库有效。
        """
        import requests

        _original_init = requests.Session.__init__

        def _patched_init(self_session, *args, **kwargs):
            _original_init(self_session, *args, **kwargs)
            self_session.trust_env = False

        # 避免重复patch
        if not getattr(requests.Session, "_trust_env_patched", False):
            requests.Session.__init__ = _patched_init
            requests.Session._trust_env_patched = True

    # ==================== 缓存 ====================

    @staticmethod
    def _cache_key(method: str, symbol: str, **kwargs) -> str:
        """生成缓存键

        格式: {method}_{symbol}_{param1}_{param2}_...
        日期和复权参数参与缓存键，确保不同参数不会混淆。
        """
        parts = [method, symbol]
        for k in ("start_date", "end_date", "adjust"):
            if k in kwargs and kwargs[k] is not None:
                parts.append(str(kwargs[k]))
        return "_".join(parts)

    def _get_with_cache(self, method: str, symbol: str, **kwargs) -> pd.DataFrame:
        """带缓存的行情数据获取

        流程: 查缓存 → 缓存命中则返回 → 缓存未命中则获取 → 写入缓存 → 返回
        """
        if self._cache_enabled:
            key = self._cache_key(method, symbol, **kwargs)
            cached = self._storage.cache_get_with_ttl(key, self._cache_ttl)
            if cached is not None:
                logger.info(f"缓存命中: {key} ({len(cached)} 条)")
                return cached

        # 缓存未命中或缓存禁用，从数据源获取
        df = self._execute_with_fallback(method, symbol, **kwargs)

        # 写入缓存
        if self._cache_enabled and df is not None and not df.empty:
            key = self._cache_key(method, symbol, **kwargs)
            try:
                self._storage.cache_set(key, df)
                logger.debug(f"已写入缓存: {key}")
            except Exception as e:
                logger.warning(f"写入缓存失败: {e}")

        return df

    # ==================== 行情数据 ====================

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    def get_daily(
        self,
        symbol: str,
        start_date: str | None = None,
        end_date: str | None = None,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        """
        获取日线行情数据

        Args:
            symbol: 股票代码, 如 "000001"
            start_date: 开始日期, 如 "20240101"
            end_date: 结束日期, 如 "20241231"
            adjust: 复权类型 qfq(前复权)/hfq(后复权)/""(不复权)

        Returns:
            DataFrame with columns: date, open, high, low, close, volume, amount
        """
        logger.debug(f"获取日线数据: {symbol}, {start_date} ~ {end_date}")
        df = self._get_with_cache(
            "get_daily", symbol, start_date=start_date, end_date=end_date, adjust=adjust
        )
        logger.debug(f"获取到 {len(df)} 条日线数据")
        return df

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    def get_weekly(
        self,
        symbol: str,
        start_date: str | None = None,
        end_date: str | None = None,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        """获取周线行情数据"""
        logger.debug(f"获取周线数据: {symbol}")
        return self._get_with_cache(
            "get_weekly", symbol, start_date=start_date, end_date=end_date, adjust=adjust
        )

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    def get_monthly(
        self,
        symbol: str,
        start_date: str | None = None,
        end_date: str | None = None,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        """获取月线行情数据"""
        logger.debug(f"获取月线数据: {symbol}")
        return self._get_with_cache(
            "get_monthly", symbol, start_date=start_date, end_date=end_date, adjust=adjust
        )

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    def get_realtime(self, symbol: str) -> pd.DataFrame:
        """
        获取实时行情

        Args:
            symbol: 股票代码

        Returns:
            实时行情数据
        """
        logger.debug(f"获取实时行情: {symbol}")
        return self._execute_with_fallback("get_realtime", symbol)

    # ==================== 基本面数据 ====================

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    def get_stock_info(self, symbol: str) -> pd.DataFrame:
        """获取股票基本信息"""
        logger.debug(f"获取股票信息: {symbol}")
        return self._execute_with_fallback("get_stock_info", symbol)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    def get_financial_summary(self, symbol: str) -> pd.DataFrame:
        """获取财务摘要数据

        支持多数据源自动降级:
          - akshare: 同花顺财务摘要 / 财务分析指标
          - baostock: 盈利数据(roeAvg, npMargin等)
        """
        logger.debug(f"获取财务摘要: {symbol}")
        return self._execute_with_fallback("get_financial_summary", symbol)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    def get_valuation(self, symbol: str) -> pd.DataFrame:
        """获取估值数据(PE/PB/PS/ROE等)

        支持多数据源自动降级:
          - akshare: stock_a_indicator_lg (PE/PB/PS/股息率/总市值)
          - baostock: history_k_data_plus (peTTM/pbMRQ/psTTM)
        """
        logger.debug(f"获取估值数据: {symbol}")
        return self._execute_with_fallback("get_valuation", symbol)

    # ==================== 板块数据 ====================

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    def get_sector_list(self) -> pd.DataFrame:
        """获取行业板块列表"""
        logger.debug("获取行业板块列表")
        return self._execute_with_fallback("get_sector_list")

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    def get_sector_stocks(self, sector: str) -> pd.DataFrame:
        """获取板块成分股"""
        logger.debug(f"获取板块成分股: {sector}")
        return self._execute_with_fallback("get_sector_stocks", sector)

    # ==================== 批量获取 ====================

    def get_multi_daily(
        self,
        symbols: list[str],
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, pd.DataFrame]:
        """
        批量获取多只股票日线数据

        Args:
            symbols: 股票代码列表
            start_date: 开始日期
            end_date: 结束日期

        Returns:
            {symbol: DataFrame} 字典
        """
        result = {}
        for symbol in symbols:
            try:
                result[symbol] = self.get_daily(symbol, start_date, end_date)
            except Exception as e:
                logger.error(f"获取 {symbol} 数据失败: {e}")
        return result
