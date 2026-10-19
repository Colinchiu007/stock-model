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
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from stock_model.config.settings import get_settings
from stock_model.data.sources.akshare_source import AkshareSource
from stock_model.data.sources.baostock_source import BaostockSource
from stock_model.data.sources.sina_source import SinaSource
from stock_model.data.storage import DataStorage

if TYPE_CHECKING:
    import pandas as pd

    from stock_model.data.sources.base import DataSource

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
    # baostock 在 Windows 上抛 RuntimeError 带中文系统消息(WinError 10054 实测)
    "远程主机强迫关闭",
    "WinError 10054",
    # baostock 错误码 10002007「网络接收错误」: 服务端/网络抖动时返回
    # (2026-10-13 凌晨实测: baostock login 直接返回它, 整个模拟盘瘫痪) ——
    # 与 baostock_source._SESSION_DEAD_PATTERNS 保持同源, 两边都要能识别。
    "10002007",
    "网络接收错误",
    # tenacity 重试耗尽抛 RetryError, 它**不是** RuntimeError/ConnectionError
    # 的子类 —— 不识别它, 数据源服务器断连时的重试耗尽就不会触发降级
    # (2026-10-11 真实故障: 前端拿到 "RetryError[<Future ...>]" 而不是降级结果)
    "RetryError",
)

TZ = ZoneInfo("Asia/Shanghai")

# A 股 15:00 收盘。日线当日不再变化, 收盘后缓存本可以留很久;
# 但盘中留太久会让"下午的盘面"停留在早上拉的数据, 所以分两档。
MARKET_CLOSE_HOUR = 15
CACHE_TTL_INTRADAY = 3600  # 盘中: 1 小时(与原 settings 默认一致)
CACHE_TTL_AFTER_CLOSE = 43200  # 收盘后: 12 小时(覆盖夜间 + 次日开盘前)


def _effective_cache_ttl(base_ttl: int, now_hour: int) -> int:
    """按当前时段选缓存 TTL

    为什么需要
    ----------
    统一 TTL 的问题是: 盘中刚拉的数据, 到收盘后正好过期, 于是
    "晚上跑定时 + 早上看盘" 和 "手动扫描" 每次都撞冷缓存。
    实测(2026-10-19): 30 只标的 冷缓存 64.2s vs 热缓存 5.0s ——
    慢的全部来自首次取数, 不是计算。

    但也不能无脑调长: 盘中留太久, 用户下午看到的还是早上的数据。
    故收盘后(``now_hour >= 15``)给长 TTL, 盘中维持短 TTL。

    ``base_ttl`` 为配置值; 只在它比盘中档更短时才拉长, 尊重显式配置。
    """
    if now_hour >= MARKET_CLOSE_HOUR:
        return max(base_ttl, CACHE_TTL_AFTER_CLOSE)
    return base_ttl


def warmup_symbols(
    fetcher: StockDataFetcher,
    symbols: list[str],
    start_date: str = "20240101",
) -> tuple[int, int]:
    """后台预热一批标的的日线缓存

    返回 ``(成功数, 失败数)``。单只失败只记录、不中断 ——
    预热是纯优化, 绝不能让一只坏标的拖垮服务启动。

    为什么需要
    ----------
    选股扫描慢在首次取数(每只 ~2s, 30 只 ~64s)。服务启动后
    静默把常用池拉一遍, 用户第一次点"开始扫描"就是热缓存(约 5s)。
    """
    ok = 0
    failed = 0
    total = len(symbols)
    logger.info(f"开始预热缓存: {total} 只 (并发=串行, baostock 单连接不可并发)")
    for i, sym in enumerate(symbols, 1):
        try:
            fetcher.get_daily(sym, start_date=start_date)
            ok += 1
            logger.debug(f"预热 {i}/{total} {sym} 成功")
        except Exception as e:  # 预热不能因单只失败而中断(单只坏标的只记录)
            failed += 1
            logger.warning(f"预热 {i}/{total} {sym} 失败: {type(e).__name__}: {e}")
    logger.info(f"预热完成: 成功 {ok} / 失败 {failed} (共 {total})")
    return ok, failed


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
    AVAILABLE_SOURCES = ("akshare", "baostock", "sina")

    def __init__(self, source: str | None = None):
        self.settings = get_settings()
        self.source = source or self.settings.data.default_source
        self._setup_proxy()
        self._source_instance: DataSource | None = None
        self._fallback_instances: list[DataSource] = []
        # 单源降级引用(向后兼容既有测试/调用); 指向降级链第一个可用源
        self._fallback_instance: DataSource | None = None
        self._init_sources()
        # 缓存
        self._storage = DataStorage()
        self._cache_enabled = self.settings.data.cache_enabled
        self._cache_ttl = self.settings.data.cache_ttl
        # 收盘后用更长的 TTL: 日线当日不再变化, 用盘中 TTL 会让
        # 「每晚定时跑 + 早上看盘」每次都撞冷缓存(实测 30 只 64s -> 5s 的差别)
        self._effective_ttl = _effective_cache_ttl(self._cache_ttl, now_hour=datetime.now(TZ).hour)
        logger.info(
            f"数据获取器初始化, 主数据源: {self.source}, "
            f"自动降级: 已启用, 缓存: {'启用' if self._cache_enabled else '禁用'} "
            f"(TTL={self._effective_ttl}s)"
        )

    def _init_sources(self) -> None:
        """初始化数据源实例(主源 + 降级链)

        降级链的由来(两次实测教训):
          - 2026-10-13: 只有 akshare→baostock 一个方向, 账户锁定 baostock
            时它挂了却切不过去, 模拟盘瘫痪。
          - 2026-10-19: baostock(10001011 黑名单/10002007) 与 akshare 底层
            (东方财富 RemoteDisconnected) **同时**不可用 —— 两个源用尽,
            选股/回测/定时推进全瘫。

        所以备选不再是"另一个源", 而是**一个有序列表**:
        主源失败逐个往后试, 任意一个成功即返回。
        某源构造失败(缺依赖/网络)只记录、不阻断其余源。
        """
        # 主源构造**也可能失败**(2026-10-19 实测: baostock 返回
        # 10001011 黑名单, 而 BaostockSource.__init__ 里就 login 并直接抛,
        # 于是 StockDataFetcher(source="baostock") 整个构造失败 ——
        # 账户落盘 data_source=baostock 时服务启动即崩, 连降级链都进不去)。
        # 故主源失败也只记录, 把可用源全放进链里继续试。
        self._primary_error: Exception | None = None
        self._source_instance = None
        try:
            self._source_instance = self._create_source(self.source)
        except (ImportError, RuntimeError, OSError, ConnectionError, ValueError) as e:
            self._primary_error = e
            logger.error(
                f"主数据源[{self.source}] 构造失败({type(e).__name__}: {e}), 将仅使用备选数据源"
            )

        self._fallback_instances = []
        for name in self._fallback_order():
            if name == self.source:
                continue
            try:
                self._fallback_instances.append(self._create_source(name))
                logger.info(f"备选数据源: {name} (自动降级已启用)")
            except (ImportError, RuntimeError, OSError, ValueError) as e:
                logger.warning(f"{name} 备选数据源初始化失败: {e}")

        # 单源引用(向后兼容): 指向降级链第一个可用源
        self._fallback_instance = self._fallback_instances[0] if self._fallback_instances else None
        # 记录 __init__ 期间自动分配的值 —— 之后若 _fallback_instance 被换成
        # 别的对象, 说明调用方显式覆盖了降级源(既有用例靠它注入假源)
        self._init_assigned_fallback = self._fallback_instance

    def _fallback_order(self) -> list[str]:
        """降级顺序: 主源之外的所有源, 按"行情可用性"优先

        sina 排前面: 它只提供行情(日/周/月线), 正好覆盖选股/回测/模拟推进
        这三条主链路, 且实测不依赖前两者的上游; 2026-10-19 两个老源同时
        挂掉时, 只有它还能出数据。
        """
        return [s for s in self.AVAILABLE_SOURCES if s != self.source]

    @staticmethod
    def _create_source(name: str) -> DataSource:
        """根据名称创建数据源实例"""
        if name == "akshare":
            return AkshareSource()
        if name == "baostock":
            return BaostockSource()
        if name == "sina":
            return SinaSource()
        raise ValueError(f"不支持的数据源: {name}, 可选: {StockDataFetcher.AVAILABLE_SOURCES}")

    @staticmethod
    def unwrap_retry_error(error: Exception) -> Exception:
        """解开 tenacity.RetryError, 返回**内层异常**(非 RetryError 原样返回)

        RetryError 的 str() 是 "RetryError[<Future at 0x... state=finished
        raised RuntimeError>]" —— 真实根因被藏在对象描述后面, 用户看到的
        是一串无意义文本。降级/报错前先解包。
        """
        last = getattr(error, "last_attempt", None)
        if last is not None:
            inner: Exception | None = getattr(last, "exception", lambda: None)()
            if isinstance(inner, BaseException):
                return inner
        return error

    @staticmethod
    def _is_connection_error(error: Exception) -> bool:
        """判断是否为连接类错误（可降级）

        同时检查错误消息和异常类名，确保ConnectionError等异常也能被正确识别。
        收到 tenacity.RetryError 时**先解出内层异常**再判定 ——
        RetryError 本身不该因为类名命中模式(那样内层是 ValueError 也会误判),
        判定的应该是**真实根因**。

        保持 staticmethod: 既有测试以 StockDataFetcher._is_connection_error(None, e) 调用。
        """
        error = StockDataFetcher.unwrap_retry_error(error)
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
        last_inner_error: Exception = RuntimeError("未知原因")
        primary_inner_error: Exception = last_inner_error
        primary_raised: Exception | None = None  # 主源抛出的原始异常(无备选时要原样抛出)
        # 尝试主数据源(构造失败时 _source_instance 为 None, 直接进降级链)
        if self._source_instance is not None:
            try:
                method = getattr(self._source_instance, method_name)
                result = method(*args, **kwargs)
                if result is not None and not result.empty:
                    return result
                # 空数据也尝试降级
                logger.warning(f"[{self.source}] {method_name} 返回空数据，尝试备选数据源")
            except Exception as e:
                # RetryError 由 _is_connection_error 内部解包判定 ——
                # 连接类错误重试耗尽必须触发降级(2026-10-11 真实故障)
                inner_e = self.unwrap_retry_error(e)
                primary_inner_error = inner_e
                last_inner_error = inner_e
                primary_raised = inner_e
                if not self._is_connection_error(e):
                    # 非连接错误，不降级，直接抛出(解包后的内层, 根因可见)
                    raise inner_e from e
                logger.warning(
                    f"[{self.source}] {method_name} 连接失败: "
                    f"{inner_e.__class__.__name__}: {inner_e}，尝试备选数据源"
                )
        # 主源构造就没成功: 记录其原因, 直接进降级链
        elif self._primary_error is not None:
            primary_inner_error = self._primary_error
            primary_raised = self._primary_error
            last_inner_error = self._primary_error
            logger.warning(
                f"[{self.source}] 主数据源不可用({type(self._primary_error).__name__}: "
                f"{self._primary_error})，尝试备选数据源"
            )

        # 依次尝试降级链上的每个源(2026-10-19: 两个老源同时挂掉,
        # 必须有第三个能顶上, 否则选股/回测/定时推进全瘫)
        #
        # 兼容两种表示:
        #   - ``_fallback_instance``(单源)若与 _init_sources 记录的不同, 说明
        #     被显式覆盖(既有用例/调用方就是用它注入假源), **优先于**列表;
        #   - 否则用 ``_fallback_instances`` 列表(正常路径);
        #   - 列表不存在时(``object.__new__`` 绕开 __init__ 的测试)回退读单源。
        chain: list[DataSource] = list(getattr(self, "_fallback_instances", None) or [])
        single = getattr(self, "_fallback_instance", None)
        init_assigned = getattr(self, "_init_assigned_fallback", None)
        if single is not None and single is not init_assigned:
            # 显式覆盖过单源: 只用它, 忽略列表(尊重调用方的指定)
            chain = [single]
        elif not chain and single is not None:
            chain = [single]

        if not chain:
            # 无备选源时, 主源的连接错误直接抛出(保留原始异常类型,
            # 不包成 RuntimeError —— 调用方按类型判断的逻辑不能被改)
            if primary_raised is not None:
                raise primary_raised
            # 主源只是返回了空数据: 无备选可试, 按"无数据"抛出
            raise RuntimeError(f"数据获取失败: 主数据源[{self.source}]返回空数据且无备选数据源")

        attempts: list[str] = []
        for fb in chain:
            name = getattr(fb, "name", fb.__class__.__name__)
            try:
                method = getattr(fb, method_name)
            except AttributeError:
                # 该源没有这个能力(如新浪没有估值接口)—— 跳过, 不是错误
                logger.info(f"[{name}] 不支持 {method_name}, 跳过")
                attempts.append(f"{name}: 不支持该能力")
                continue
            try:
                result = method(*args, **kwargs)
                if result is not None and not result.empty:
                    logger.info(f"[{name}] {method_name} 降级获取成功")
                    return result
                logger.warning(f"[{name}] {method_name} 返回空数据")
                attempts.append(f"{name}: 返回空数据")
            except Exception as fallback_error:
                # 加 RetryError 并解包: 备选源也可能用 tenacity(漏捕会穿透给用户)
                inner = self.unwrap_retry_error(fallback_error)
                last_inner_error = inner
                logger.error(f"[{name}] {method_name} 也失败: {inner.__class__.__name__}: {inner}")
                attempts.append(f"{name}: {inner.__class__.__name__}: {inner}")

        # 根因**拼进消息文本**(而不是只挂在异常链上):
        # 前端展示的就是 str(e), 异常链用户看不到
        tried = "; ".join(attempts) if attempts else "无备选数据源"
        raise RuntimeError(
            f"数据获取失败: 主数据源[{self.source}]和备选数据源均无法获取数据 "
            f"(主源原因: {primary_inner_error.__class__.__name__}: "
            f"{primary_inner_error}; 各备选源: {tried})"
        ) from last_inner_error

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
        #
        # mypy: 这是在给第三方类打猴子补丁 —— 赋值 __init__ 与新增私有标记属性
        # 本来就超出 requests 的类型声明, 三个错误都是"补丁本身"的必然结果,
        # 不是缺陷。故按错误码精确豁免, 不用整段 ignore。
        #
        # ⚠️ 本地 requests 较旧(不带 py.typed)时**看不到**这些错误;
        # CI 的 requests 2.34+ 自带类型信息, 才会报出来。
        # 也就是说 `ignore_missing_imports` 只豁免"缺 stub", 不豁免
        # "stub 在场时的真实错误" —— 以 CI 为准。
        if not getattr(requests.Session, "_trust_env_patched", False):
            requests.Session.__init__ = _patched_init  # type: ignore[method-assign, assignment]
            requests.Session._trust_env_patched = True  # type: ignore[attr-defined]

    # ==================== 缓存 ====================

    @staticmethod
    def _cache_key(method: str, symbol: str, **kwargs) -> str:
        """生成缓存键

        格式: {method}_{symbol}_{param1}_{param2}_...
        日期和复权参数参与缓存键，确保不同参数不会混淆。
        """
        parts = [method, symbol]
        parts.extend(
            str(kwargs[k])
            for k in ("start_date", "end_date", "adjust")
            if k in kwargs and kwargs[k] is not None
        )
        return "_".join(parts)

    def _get_with_cache(self, method: str, symbol: str, **kwargs) -> pd.DataFrame:
        """带缓存的行情数据获取

        流程: 查缓存 → 缓存命中则返回 → 缓存未命中则获取 → 写入缓存 → 返回

        缓存是优化而非主路径: 缓存层出现任何异常(缺可选引擎/磁盘故障等)
        都不得影响数据获取本身。
        """
        if self._cache_enabled:
            key = self._cache_key(method, symbol, **kwargs)
            try:
                cached = self._storage.cache_get_with_ttl(key, self._effective_ttl)
                if cached is not None:
                    logger.info(f"缓存命中: {key} ({len(cached)} 条)")
                    return cached
            except (OSError, ValueError, ImportError, AttributeError) as e:
                logger.warning(f"读取缓存失败({key}): {e}, 继续走数据源")

        # 缓存未命中或缓存禁用，从数据源获取
        df = self._execute_with_fallback(method, symbol, **kwargs)

        # 写入缓存
        if self._cache_enabled and df is not None and not df.empty:
            key = self._cache_key(method, symbol, **kwargs)
            try:
                self._storage.cache_set(key, df)
                logger.debug(f"已写入缓存: {key}")
            except (OSError, ValueError, ImportError, AttributeError) as e:
                # 缓存写不进去不影响返回数据
                logger.warning(f"写入缓存失败({key}): {e}")

        return df

    # ==================== 行情数据 ====================

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10), reraise=True)
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

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10), reraise=True)
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

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10), reraise=True)
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

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10), reraise=True)
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

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10), reraise=True)
    def get_stock_info(self, symbol: str) -> pd.DataFrame:
        """获取股票基本信息"""
        logger.debug(f"获取股票信息: {symbol}")
        return self._execute_with_fallback("get_stock_info", symbol)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10), reraise=True)
    def get_financial_summary(self, symbol: str) -> pd.DataFrame:
        """获取财务摘要数据

        支持多数据源自动降级:
          - akshare: 同花顺财务摘要 / 财务分析指标
          - baostock: 盈利数据(roeAvg, npMargin等)
        """
        logger.debug(f"获取财务摘要: {symbol}")
        return self._execute_with_fallback("get_financial_summary", symbol)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10), reraise=True)
    def get_valuation(self, symbol: str) -> pd.DataFrame:
        """获取估值数据(PE/PB/PS/ROE等)

        支持多数据源自动降级:
          - akshare: stock_a_indicator_lg (PE/PB/PS/股息率/总市值)
          - baostock: history_k_data_plus (peTTM/pbMRQ/psTTM)
        """
        logger.debug(f"获取估值数据: {symbol}")
        return self._execute_with_fallback("get_valuation", symbol)

    # ==================== 板块数据 ====================

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10), reraise=True)
    def get_sector_list(self) -> pd.DataFrame:
        """获取行业板块列表"""
        logger.debug("获取行业板块列表")
        return self._execute_with_fallback("get_sector_list")

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10), reraise=True)
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
            except (ValueError, KeyError, ConnectionError) as e:
                logger.error(f"获取 {symbol} 数据失败: {e}")
        return result
