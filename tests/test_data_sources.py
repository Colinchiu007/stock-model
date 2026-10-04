"""多数据源架构单元测试

测试内容:
  - BaostockSource 静态方法 (代码转换/日期转换/复权转换)
  - StockDataFetcher 连接错误检测
  - StockDataFetcher 数据源创建
  - 自动降级机制 (mock测试)
  - 缓存机制 (缓存键/缓存命中/缓存过期/缓存禁用)
  - 估值数据获取 (get_valuation)
  - 财务摘要获取 (get_financial_summary)
  - DataSource基类新增抽象方法
"""

from unittest.mock import MagicMock

import pandas as pd
import pytest

from stock_model.data.fetcher import StockDataFetcher
from stock_model.data.sources.akshare_source import AkshareSource
from stock_model.data.sources.baostock_source import BaostockSource
from stock_model.data.sources.base import DataSource

# ============================================================
# BaostockSource 静态方法测试
# ============================================================


class TestBaostockConvertSymbol:
    """股票代码转换测试"""

    def test_shenzhen_main(self):
        """深市主板: 0开头"""
        assert BaostockSource._convert_symbol("000001") == "sz.000001"

    def test_shenzhen_gem(self):
        """创业板: 3开头"""
        assert BaostockSource._convert_symbol("300001") == "sz.300001"

    def test_shenzhen_small(self):
        """中小板: 0/1/2开头"""
        assert BaostockSource._convert_symbol("002001") == "sz.002001"

    def test_shanghai_main(self):
        """沪市主板: 6开头"""
        assert BaostockSource._convert_symbol("600000") == "sh.600000"

    def test_shanghai_star(self):
        """科创板: 688开头 (6开头)"""
        assert BaostockSource._convert_symbol("688001") == "sh.688001"

    def test_bse(self):
        """北交所: 8开头"""
        assert BaostockSource._convert_symbol("830001") == "bj.830001"

    def test_already_prefixed_sh(self):
        """已有sh.前缀直接返回"""
        assert BaostockSource._convert_symbol("sh.600000") == "sh.600000"

    def test_already_prefixed_sz(self):
        """已有sz.前缀直接返回"""
        assert BaostockSource._convert_symbol("sz.000001") == "sz.000001"

    def test_unknown_prefix(self):
        """未知前缀默认sz."""
        assert BaostockSource._convert_symbol("500001") == "sz.500001"


class TestBaostockNormalizeDate:
    """日期格式转换测试"""

    def test_yyyymmdd_format(self):
        """8位纯数字 -> YYYY-MM-DD"""
        assert BaostockSource._normalize_date("20240101") == "2024-01-01"

    def test_already_normalized(self):
        """已有横线格式直接返回"""
        assert BaostockSource._normalize_date("2024-01-01") == "2024-01-01"

    def test_none_input(self):
        """None输入返回None"""
        assert BaostockSource._normalize_date(None) is None

    def test_empty_string(self):
        """空字符串直接返回"""
        assert BaostockSource._normalize_date("") == ""

    def test_short_date(self):
        """不足8位直接返回"""
        assert BaostockSource._normalize_date("2024") == "2024"

    def test_with_spaces(self):
        """带空格先trim再处理"""
        assert BaostockSource._normalize_date(" 20240101 ") == "2024-01-01"


class TestBaostockConvertAdjust:
    """复权标识转换测试"""

    def test_qfq(self):
        """前复权"""
        assert BaostockSource._convert_adjust("qfq") == "2"

    def test_hfq(self):
        """后复权"""
        assert BaostockSource._convert_adjust("hfq") == "1"

    def test_no_adjust(self):
        """不复权"""
        assert BaostockSource._convert_adjust("") == "3"

    def test_unknown_defaults_qfq(self):
        """未知值默认前复权"""
        assert BaostockSource._convert_adjust("unknown") == "2"


# ============================================================
# DataSource 抽象基类测试
# ============================================================


class TestDataSourceBase:
    """数据源抽象基类测试"""

    def test_cannot_instantiate(self):
        """不能直接实例化抽象基类"""
        with pytest.raises(TypeError):
            DataSource()

    def test_baostock_is_datasource(self):
        """BaostockSource是DataSource的子类"""
        assert issubclass(BaostockSource, DataSource)

    def test_akshare_is_datasource(self):
        """AkshareSource是DataSource的子类"""
        assert issubclass(AkshareSource, DataSource)


# ============================================================
# StockDataFetcher 测试
# ============================================================


class TestConnectionErrorDetection:
    """连接错误检测测试"""

    def test_connection_error_pattern(self):
        """ConnectionError可被检测"""
        err = ConnectionError("Connection refused")
        assert StockDataFetcher._is_connection_error(None, err) is True

    def test_remote_disconnected(self):
        """RemoteDisconnected可被检测"""
        from http.client import RemoteDisconnected

        err = RemoteDisconnected("Remote end closed connection without response")
        assert StockDataFetcher._is_connection_error(None, err) is True

    def test_proxy_error(self):
        """ProxyError可被检测 (包含Max retries exceeded)"""
        err = Exception("Max retries exceeded with url: /api/test")
        assert StockDataFetcher._is_connection_error(None, err) is True

    def test_ssl_error(self):
        """SSLError可被检测"""
        err = Exception("SSLError: SSL handshake failed")
        assert StockDataFetcher._is_connection_error(None, err) is True

    def test_non_connection_error(self):
        """非连接错误不触发降级"""
        err = ValueError("Invalid stock code")
        assert StockDataFetcher._is_connection_error(None, err) is False

    def test_runtime_error_not_connection(self):
        """RuntimeError不是连接错误"""
        err = RuntimeError("Data processing failed")
        assert StockDataFetcher._is_connection_error(None, err) is False

    def test_key_error_not_connection(self):
        """KeyError不是连接错误"""
        err = KeyError("Missing column: close")
        assert StockDataFetcher._is_connection_error(None, err) is False


class TestCreateSource:
    """数据源创建测试"""

    def test_create_akshare(self):
        """创建akshare数据源"""
        source = StockDataFetcher._create_source("akshare")
        assert isinstance(source, AkshareSource)
        assert source.name == "akshare"

    def test_create_baostock(self):
        """创建baostock数据源"""
        source = StockDataFetcher._create_source("baostock")
        assert isinstance(source, BaostockSource)
        assert source.name == "baostock"

    def test_create_unknown_raises(self):
        """创建未知数据源抛出ValueError"""
        with pytest.raises(ValueError, match="不支持的数据源"):
            StockDataFetcher._create_source("unknown")


class TestFallbackMechanism:
    """自动降级机制测试 (使用mock)"""

    def _make_fetcher_with_mocks(
        self, primary_error=None, primary_empty=False, fallback_result=None, fallback_error=None
    ):
        """创建带有mock数据源的fetcher"""
        fetcher = object.__new__(StockDataFetcher)
        fetcher.settings = MagicMock()
        fetcher.source = "akshare"

        # Mock主数据源
        fetcher._source_instance = MagicMock(spec=DataSource)
        fetcher._source_instance.name = "akshare"
        if primary_error:
            fetcher._source_instance.get_daily.side_effect = primary_error
        elif primary_empty:
            fetcher._source_instance.get_daily.return_value = pd.DataFrame()
        else:
            fetcher._source_instance.get_daily.return_value = pd.DataFrame(
                {"close": [10.0]}, index=pd.to_datetime(["2024-01-01"])
            )

        # Mock备选数据源
        fetcher._fallback_instance = MagicMock(spec=DataSource)
        fetcher._fallback_instance.name = "baostock"
        if fallback_error:
            fetcher._fallback_instance.get_daily.side_effect = fallback_error
        elif fallback_result is not None:
            fetcher._fallback_instance.get_daily.return_value = fallback_result
        else:
            fetcher._fallback_instance.get_daily.return_value = pd.DataFrame(
                {"close": [11.0]}, index=pd.to_datetime(["2024-01-01"])
            )

        return fetcher

    def test_primary_success_no_fallback(self):
        """主数据源成功时不降级"""
        fetcher = self._make_fetcher_with_mocks()
        result = fetcher._execute_with_fallback("get_daily", "000001")
        assert len(result) == 1
        fetcher._fallback_instance.get_daily.assert_not_called()

    def test_connection_error_triggers_fallback(self):
        """连接错误触发降级"""
        fetcher = self._make_fetcher_with_mocks(primary_error=ConnectionError("Connection refused"))
        result = fetcher._execute_with_fallback("get_daily", "000001")
        assert len(result) == 1
        fetcher._fallback_instance.get_daily.assert_called_once()

    def test_empty_data_triggers_fallback(self):
        """空数据触发降级"""
        fetcher = self._make_fetcher_with_mocks(primary_empty=True)
        result = fetcher._execute_with_fallback("get_daily", "000001")
        assert len(result) == 1
        fetcher._fallback_instance.get_daily.assert_called_once()

    def test_non_connection_error_raises(self):
        """非连接错误直接抛出, 不降级"""
        fetcher = self._make_fetcher_with_mocks(primary_error=ValueError("Invalid symbol"))
        with pytest.raises(ValueError, match="Invalid symbol"):
            fetcher._execute_with_fallback("get_daily", "000001")
        fetcher._fallback_instance.get_daily.assert_not_called()

    def test_both_fail_raises_runtime_error(self):
        """主备都失败抛出RuntimeError"""
        fetcher = self._make_fetcher_with_mocks(
            primary_error=ConnectionError("Connection refused"),
            fallback_error=RuntimeError("baostock also failed"),
        )
        with pytest.raises(RuntimeError, match="数据获取失败"):
            fetcher._execute_with_fallback("get_daily", "000001")

    def test_no_fallback_instance_primary_fails(self):
        """无备选数据源时, 连接错误直接抛出"""
        fetcher = self._make_fetcher_with_mocks(primary_error=ConnectionError("Connection refused"))
        fetcher._fallback_instance = None
        with pytest.raises(ConnectionError):
            fetcher._execute_with_fallback("get_daily", "000001")


# ============================================================
# BaostockSource 数据标准化测试
# ============================================================


class TestBaostockNormalize:
    """数据标准化测试"""

    def test_column_rename(self):
        """列名映射正确"""
        df = pd.DataFrame(
            {
                "date": ["2024-01-01"],
                "open": ["10.0"],
                "high": ["11.0"],
                "low": ["9.0"],
                "close": ["10.5"],
                "volume": ["1000000"],
                "amount": ["10500000"],
                "turn": ["1.5"],
                "pctChg": ["5.0"],
            }
        )
        result = BaostockSource._normalize(df, "000001")
        assert "turnover" in result.columns
        assert "pct_change" in result.columns
        assert "turn" not in result.columns
        assert "pctChg" not in result.columns

    def test_numeric_conversion(self):
        """字符串转数值"""
        df = pd.DataFrame(
            {
                "date": ["2024-01-01"],
                "open": ["10.5"],
                "close": ["10.8"],
            }
        )
        result = BaostockSource._normalize(df, "000001")
        assert result["open"].dtype in ("float64", "int64")
        assert result["close"].dtype in ("float64", "int64")

    def test_date_index(self):
        """日期设置为索引"""
        df = pd.DataFrame(
            {
                "date": ["2024-01-01", "2024-01-02"],
                "close": ["10.0", "11.0"],
            }
        )
        result = BaostockSource._normalize(df, "000001")
        assert result.index.name == "date"
        assert isinstance(result.index, pd.DatetimeIndex)

    def test_symbol_added(self):
        """添加symbol列"""
        df = pd.DataFrame(
            {
                "date": ["2024-01-01"],
                "close": ["10.0"],
            }
        )
        result = BaostockSource._normalize(df, "600000")
        assert "symbol" in result.columns
        assert result["symbol"].iloc[0] == "600000"


# ============================================================
# 缓存机制测试
# ============================================================


class TestCacheKey:
    """缓存键生成测试"""

    def test_basic_key(self):
        """基本缓存键格式"""
        key = StockDataFetcher._cache_key("get_daily", "000001")
        assert key == "get_daily_000001"

    def test_key_with_start_date(self):
        """包含开始日期的缓存键"""
        key = StockDataFetcher._cache_key("get_daily", "000001", start_date="20240101")
        assert key == "get_daily_000001_20240101"

    def test_key_with_all_params(self):
        """包含所有参数的缓存键"""
        key = StockDataFetcher._cache_key(
            "get_daily", "600000", start_date="20240101", end_date="20241231", adjust="qfq"
        )
        assert key == "get_daily_600000_20240101_20241231_qfq"

    def test_key_none_params_excluded(self):
        """None参数不参与缓存键"""
        key1 = StockDataFetcher._cache_key("get_daily", "000001", start_date=None, end_date=None)
        key2 = StockDataFetcher._cache_key("get_daily", "000001")
        assert key1 == key2

    def test_different_methods_different_keys(self):
        """不同方法生成不同缓存键"""
        key_daily = StockDataFetcher._cache_key("get_daily", "000001")
        key_weekly = StockDataFetcher._cache_key("get_weekly", "000001")
        assert key_daily != key_weekly

    def test_different_symbols_different_keys(self):
        """不同股票代码生成不同缓存键"""
        key1 = StockDataFetcher._cache_key("get_daily", "000001")
        key2 = StockDataFetcher._cache_key("get_daily", "600000")
        assert key1 != key2


class TestCacheIntegration:
    """缓存集成测试 (mock DataStorage)"""

    def _make_fetcher_with_cache(self, cache_enabled=True, cache_ttl=3600):
        """创建带缓存mock的fetcher"""
        fetcher = object.__new__(StockDataFetcher)
        fetcher.settings = MagicMock()
        fetcher.source = "baostock"
        fetcher._source_instance = MagicMock()
        fetcher._fallback_instance = None
        fetcher._storage = MagicMock()
        fetcher._cache_enabled = cache_enabled
        fetcher._cache_ttl = cache_ttl
        return fetcher

    def test_cache_hit_returns_cached_data(self):
        """缓存命中时直接返回缓存数据"""
        fetcher = self._make_fetcher_with_cache()
        cached_df = pd.DataFrame({"close": [10.0]}, index=pd.to_datetime(["2024-01-01"]))
        fetcher._storage.cache_get_with_ttl.return_value = cached_df

        result = fetcher._get_with_cache("get_daily", "000001")
        assert len(result) == 1
        fetcher._source_instance.get_daily.assert_not_called()

    def test_cache_miss_fetches_and_stores(self):
        """缓存未命中时获取数据并写入缓存"""
        fetcher = self._make_fetcher_with_cache()
        fetcher._storage.cache_get_with_ttl.return_value = None
        fresh_df = pd.DataFrame({"close": [11.0]}, index=pd.to_datetime(["2024-01-01"]))
        fetcher._source_instance.get_daily.return_value = fresh_df

        result = fetcher._get_with_cache("get_daily", "000001")
        assert len(result) == 1
        fetcher._source_instance.get_daily.assert_called_once()
        fetcher._storage.cache_set.assert_called_once()

    def test_cache_disabled_skips_cache(self):
        """缓存禁用时不读写缓存"""
        fetcher = self._make_fetcher_with_cache(cache_enabled=False)
        fresh_df = pd.DataFrame({"close": [11.0]}, index=pd.to_datetime(["2024-01-01"]))
        fetcher._source_instance.get_daily.return_value = fresh_df

        result = fetcher._get_with_cache("get_daily", "000001")
        assert len(result) == 1
        fetcher._storage.cache_get_with_ttl.assert_not_called()
        fetcher._storage.cache_set.assert_not_called()

    def test_cache_write_failure_does_not_raise(self):
        """缓存写入失败不影响返回数据"""
        fetcher = self._make_fetcher_with_cache()
        fetcher._storage.cache_get_with_ttl.return_value = None
        fetcher._storage.cache_set.side_effect = OSError("disk full")
        fresh_df = pd.DataFrame({"close": [11.0]}, index=pd.to_datetime(["2024-01-01"]))
        fetcher._source_instance.get_daily.return_value = fresh_df

        result = fetcher._get_with_cache("get_daily", "000001")
        assert len(result) == 1

    def test_empty_data_not_cached(self):
        """空数据不写入缓存"""
        fetcher = self._make_fetcher_with_cache()
        fetcher._storage.cache_get_with_ttl.return_value = None
        fetcher._source_instance.get_daily.return_value = pd.DataFrame()

        with pytest.raises(RuntimeError):
            fetcher._get_with_cache("get_daily", "000001")
        fetcher._storage.cache_set.assert_not_called()

    def test_cache_ttl_passed_correctly(self):
        """TTL参数正确传递"""
        fetcher = self._make_fetcher_with_cache(cache_ttl=1800)
        fetcher._storage.cache_get_with_ttl.return_value = None
        fresh_df = pd.DataFrame({"close": [11.0]}, index=pd.to_datetime(["2024-01-01"]))
        fetcher._source_instance.get_daily.return_value = fresh_df

        fetcher._get_with_cache("get_daily", "000001")
        fetcher._storage.cache_get_with_ttl.assert_called_once()
        call_args = fetcher._storage.cache_get_with_ttl.call_args
        assert call_args[0][1] == 1800


# ============================================================
# 估值数据获取测试
# ============================================================


class TestValuationFallback:
    """估值数据获取自动降级测试"""

    def _make_fetcher(self):
        """创建mock fetcher用于降级测试"""
        fetcher = object.__new__(StockDataFetcher)
        fetcher.settings = MagicMock()
        fetcher.source = "akshare"
        fetcher._source_instance = MagicMock()
        fetcher._fallback_instance = MagicMock()
        fetcher._storage = MagicMock()
        fetcher._cache_enabled = False
        fetcher._cache_ttl = 3600
        return fetcher

    def test_valuation_primary_success(self):
        """主数据源获取估值数据成功"""
        fetcher = self._make_fetcher()
        valuation_df = pd.DataFrame(
            {"pe_ttm": [15.0], "pb": [2.0]},
            index=pd.to_datetime(["2024-01-01"]),
        )
        fetcher._source_instance.get_valuation.return_value = valuation_df

        result = fetcher._execute_with_fallback("get_valuation", "000001")
        assert len(result) == 1
        fetcher._source_instance.get_valuation.assert_called_once_with("000001")
        fetcher._fallback_instance.get_valuation.assert_not_called()

    def test_valuation_fallback_on_connection_error(self):
        """主数据源连接错误时自动降级到备选"""
        fetcher = self._make_fetcher()
        fetcher._source_instance.get_valuation.side_effect = ConnectionError("TLS handshake failed")
        valuation_df = pd.DataFrame(
            {"pe_ttm": [15.0], "pb": [2.0]},
            index=pd.to_datetime(["2024-01-01"]),
        )
        fetcher._fallback_instance.get_valuation.return_value = valuation_df

        result = fetcher._execute_with_fallback("get_valuation", "000001")
        assert len(result) == 1
        fetcher._fallback_instance.get_valuation.assert_called_once_with("000001")

    def test_valuation_both_fail_raises(self):
        """两个数据源都失败时抛出RuntimeError"""
        fetcher = self._make_fetcher()
        fetcher._source_instance.get_valuation.side_effect = ConnectionError("failed")
        fetcher._fallback_instance.get_valuation.side_effect = ConnectionError("also failed")

        with pytest.raises(RuntimeError, match="数据获取失败"):
            fetcher._execute_with_fallback("get_valuation", "000001")


# ============================================================
# 财务摘要获取测试
# ============================================================


class TestFinancialSummaryFallback:
    """财务摘要获取自动降级测试"""

    def _make_fetcher(self):
        """创建mock fetcher"""
        fetcher = object.__new__(StockDataFetcher)
        fetcher.settings = MagicMock()
        fetcher.source = "akshare"
        fetcher._source_instance = MagicMock()
        fetcher._fallback_instance = MagicMock()
        fetcher._storage = MagicMock()
        fetcher._cache_enabled = False
        fetcher._cache_ttl = 3600
        return fetcher

    def test_financial_summary_primary_success(self):
        """主数据源获取财务摘要成功"""
        fetcher = self._make_fetcher()
        summary_df = pd.DataFrame(
            {"roeAvg": [12.5], "npMargin": [15.0]},
            index=[0],
        )
        fetcher._source_instance.get_financial_summary.return_value = summary_df

        result = fetcher._execute_with_fallback("get_financial_summary", "000001")
        assert len(result) == 1
        fetcher._source_instance.get_financial_summary.assert_called_once_with("000001")

    def test_financial_summary_fallback_on_error(self):
        """主数据源失败时自动降级"""
        fetcher = self._make_fetcher()
        fetcher._source_instance.get_financial_summary.side_effect = ConnectionError("timeout")
        summary_df = pd.DataFrame(
            {"roeAvg": [10.0], "npMargin": [12.0]},
            index=[0],
        )
        fetcher._fallback_instance.get_financial_summary.return_value = summary_df

        result = fetcher._execute_with_fallback("get_financial_summary", "000001")
        assert len(result) == 1
        fetcher._fallback_instance.get_financial_summary.assert_called_once_with("000001")


# ============================================================
# DataSource基类新增抽象方法测试
# ============================================================


class TestDataSourceAbstractMethods:
    """DataSource基类新增抽象方法验证"""

    def test_get_valuation_is_abstract(self):
        """get_valuation是抽象方法，子类必须实现"""
        # AkshareSource和BostockSource都已实现，不会报错
        assert hasattr(AkshareSource, "get_valuation")
        assert hasattr(BaostockSource, "get_valuation")

    def test_get_financial_summary_is_abstract(self):
        """get_financial_summary是抽象方法，子类必须实现"""
        assert hasattr(AkshareSource, "get_financial_summary")
        assert hasattr(BaostockSource, "get_financial_summary")

    def test_incomplete_subclass_raises_type_error(self):
        """不完整的子类无法实例化"""

        class IncompleteSource(DataSource):
            name = "incomplete"

            def get_daily(self, symbol, start_date=None, end_date=None, adjust="qfq"):
                pass

            def get_weekly(self, symbol, start_date=None, end_date=None, adjust="qfq"):
                pass

            def get_monthly(self, symbol, start_date=None, end_date=None, adjust="qfq"):
                pass

            def get_realtime(self, symbol):
                pass

            def get_stock_info(self, symbol):
                pass

            def get_sector_list(self):
                pass

            def get_sector_stocks(self, sector):
                pass

            # 缺少 get_valuation 和 get_financial_summary

        with pytest.raises(TypeError):
            IncompleteSource()
