"""BaostockSource 数据源测试"""

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from stock_model.data.sources.baostock_source import BaostockSource


def _mock_login_result(error_code="0", error_msg=""):
    """构造模拟baostock login结果"""
    lg = MagicMock()
    lg.error_code = error_code
    lg.error_msg = error_msg
    return lg


def _mock_query_result(data=None, fields=None, error_code="0", error_msg=""):
    """构造模拟baostock query结果"""
    rs = MagicMock()
    rs.error_code = error_code
    rs.error_msg = error_msg
    default_fields = [
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "turn",
        "pctChg",
    ]
    rs.fields = fields or default_fields
    _data = data or []
    _idx = [0]

    def mock_next():
        if _idx[0] < len(_data):
            _idx[0] += 1
            return True
        return False

    def mock_get_row_data():
        if _idx[0] > 0 and _idx[0] <= len(_data):
            return _data[_idx[0] - 1]
        return []

    rs.next = mock_next
    rs.get_row_data = mock_get_row_data
    # 重置索引
    _idx[0] = 0
    return rs


def _make_kline_data(n=3):
    """构造模拟K线数据行"""
    return [
        [
            f"2024-01-0{i + 1}",
            str(10 + i),
            str(11 + i),
            str(9 + i),
            str(10.5 + i),
            "1000",
            "10000",
            "1.5",
            "0.5",
        ]
        for i in range(n)
    ]


class TestBaostockLogin:
    """登录/登出测试"""

    @patch("stock_model.data.sources.baostock_source.bs")
    def test_init_login_success(self, mock_bs):
        """初始化时自动登录"""
        mock_bs.login.return_value = _mock_login_result()
        # 重置类变量
        BaostockSource._logged_in = False
        BaostockSource()
        mock_bs.login.assert_called_once()
        assert BaostockSource._logged_in is True
        # 清理
        BaostockSource._logged_in = False

    @patch("stock_model.data.sources.baostock_source.bs")
    def test_init_login_failure(self, mock_bs):
        """登录失败抛出ConnectionError"""
        mock_bs.login.return_value = _mock_login_result(error_code="1", error_msg="connect failed")
        BaostockSource._logged_in = False
        with pytest.raises(ConnectionError, match="baostock登录失败"):
            BaostockSource()
        BaostockSource._logged_in = False

    @patch("stock_model.data.sources.baostock_source.bs")
    def test_logout(self, mock_bs):
        """登出成功"""
        BaostockSource._logged_in = True
        BaostockSource.logout()
        mock_bs.logout.assert_called_once()
        assert BaostockSource._logged_in is False

    @patch("stock_model.data.sources.baostock_source.bs")
    def test_logout_not_logged_in(self, mock_bs):
        """未登录时登出不调用bs.logout"""
        BaostockSource._logged_in = False
        BaostockSource.logout()
        mock_bs.logout.assert_not_called()


class TestGetDaily:
    """get_daily 日线数据测试"""

    @patch("stock_model.data.sources.baostock_source.bs")
    def test_get_daily_success(self, mock_bs):
        mock_bs.login.return_value = _mock_login_result()
        mock_bs.query_history_k_data_plus.return_value = _mock_query_result(
            data=_make_kline_data(),
            fields=[
                "date",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "amount",
                "turn",
                "pctChg",
            ],
        )
        BaostockSource._logged_in = False
        src = BaostockSource()
        df = src.get_daily("000001")
        assert len(df) == 3
        assert "symbol" in df.columns
        assert df["symbol"].iloc[0] == "000001"
        BaostockSource._logged_in = False

    @patch("stock_model.data.sources.baostock_source.bs")
    def test_get_daily_query_error(self, mock_bs):
        mock_bs.login.return_value = _mock_login_result()
        mock_bs.query_history_k_data_plus.return_value = _mock_query_result(
            error_code="1", error_msg="query failed"
        )
        BaostockSource._logged_in = False
        src = BaostockSource()
        with pytest.raises(RuntimeError, match="baostock查询失败"):
            src.get_daily("000001")
        BaostockSource._logged_in = False

    @patch("stock_model.data.sources.baostock_source.bs")
    def test_get_daily_no_data(self, mock_bs):
        mock_bs.login.return_value = _mock_login_result()
        mock_bs.query_history_k_data_plus.return_value = _mock_query_result(data=[])
        BaostockSource._logged_in = False
        src = BaostockSource()
        df = src.get_daily("000001")
        assert df.empty
        BaostockSource._logged_in = False


class TestGetWeekly:
    """get_weekly 周线数据测试"""

    @patch("stock_model.data.sources.baostock_source.bs")
    def test_get_weekly_success(self, mock_bs):
        mock_bs.login.return_value = _mock_login_result()
        mock_bs.query_history_k_data_plus.return_value = _mock_query_result(data=_make_kline_data())
        BaostockSource._logged_in = False
        src = BaostockSource()
        df = src.get_weekly("600000")
        assert len(df) == 3
        BaostockSource._logged_in = False

    @patch("stock_model.data.sources.baostock_source.bs")
    def test_get_weekly_no_data(self, mock_bs):
        mock_bs.login.return_value = _mock_login_result()
        mock_bs.query_history_k_data_plus.return_value = _mock_query_result(data=[])
        BaostockSource._logged_in = False
        src = BaostockSource()
        df = src.get_weekly("600000")
        assert df.empty
        BaostockSource._logged_in = False


class TestGetMonthly:
    """get_monthly 月线数据测试"""

    @patch("stock_model.data.sources.baostock_source.bs")
    def test_get_monthly_success(self, mock_bs):
        mock_bs.login.return_value = _mock_login_result()
        mock_bs.query_history_k_data_plus.return_value = _mock_query_result(data=_make_kline_data())
        BaostockSource._logged_in = False
        src = BaostockSource()
        df = src.get_monthly("300001")
        assert len(df) == 3
        BaostockSource._logged_in = False


class TestGetRealtime:
    """get_realtime 实时行情测试(返回最近日线)"""

    @patch("stock_model.data.sources.baostock_source.bs")
    def test_get_realtime_returns_daily(self, mock_bs):
        mock_bs.login.return_value = _mock_login_result()
        mock_bs.query_history_k_data_plus.return_value = _mock_query_result(data=_make_kline_data())
        BaostockSource._logged_in = False
        src = BaostockSource()
        df = src.get_realtime("000001")
        assert len(df) == 3
        BaostockSource._logged_in = False


class TestGetStockInfo:
    """get_stock_info 股票信息测试"""

    @patch("stock_model.data.sources.baostock_source.bs")
    def test_get_stock_info_success(self, mock_bs):
        mock_bs.login.return_value = _mock_login_result()
        industry_data = [["2024-01-01", "sz.000001", "平安银行", "银行", "金融"]]
        mock_bs.query_stock_industry.return_value = _mock_query_result(
            data=industry_data,
            fields=["updateDate", "code", "code_name", "industry", "industryClassification"],
        )
        BaostockSource._logged_in = False
        src = BaostockSource()
        df = src.get_stock_info("000001")
        assert len(df) == 1
        BaostockSource._logged_in = False

    @patch("stock_model.data.sources.baostock_source.bs")
    def test_get_stock_info_not_found(self, mock_bs):
        mock_bs.login.return_value = _mock_login_result()
        # 返回不匹配的数据
        industry_data = [["2024-01-01", "sh.600000", "浦发银行", "银行", "金融"]]
        mock_bs.query_stock_industry.return_value = _mock_query_result(
            data=industry_data,
            fields=["updateDate", "code", "code_name", "industry", "industryClassification"],
        )
        BaostockSource._logged_in = False
        src = BaostockSource()
        df = src.get_stock_info("000001")
        assert df.empty
        BaostockSource._logged_in = False


class TestGetSectorList:
    """get_sector_list 行业板块测试"""

    @patch("stock_model.data.sources.baostock_source.bs")
    def test_get_sector_list_success(self, mock_bs):
        mock_bs.login.return_value = _mock_login_result()
        industry_data = [
            ["2024-01-01", "sz.000001", "平安银行", "银行", "金融"],
            ["2024-01-01", "sh.600000", "浦发银行", "银行", "金融"],
            ["2024-01-01", "sz.000002", "万科A", "地产", "房地产"],
        ]
        mock_bs.query_stock_industry.return_value = _mock_query_result(
            data=industry_data,
            fields=["updateDate", "code", "code_name", "industry", "industryClassification"],
        )
        BaostockSource._logged_in = False
        src = BaostockSource()
        df = src.get_sector_list()
        assert "industry" in df.columns
        BaostockSource._logged_in = False

    @patch("stock_model.data.sources.baostock_source.bs")
    def test_get_sector_list_query_error(self, mock_bs):
        mock_bs.login.return_value = _mock_login_result()
        mock_bs.query_stock_industry.return_value = _mock_query_result(
            error_code="1", error_msg="failed"
        )
        BaostockSource._logged_in = False
        src = BaostockSource()
        with pytest.raises(RuntimeError, match="baostock查询行业失败"):
            src.get_sector_list()
        BaostockSource._logged_in = False


class TestGetSectorStocks:
    """get_sector_stocks 板块成分股测试"""

    @patch("stock_model.data.sources.baostock_source.bs")
    def test_get_sector_stocks_success(self, mock_bs):
        mock_bs.login.return_value = _mock_login_result()
        industry_data = [
            ["2024-01-01", "sz.000001", "平安银行", "银行", "金融"],
            ["2024-01-01", "sh.600000", "浦发银行", "银行", "金融"],
        ]
        mock_bs.query_stock_industry.return_value = _mock_query_result(
            data=industry_data,
            fields=["updateDate", "code", "code_name", "industry", "industryClassification"],
        )
        BaostockSource._logged_in = False
        src = BaostockSource()
        df = src.get_sector_stocks("银行")
        assert len(df) == 2
        BaostockSource._logged_in = False


class TestGetValuation:
    """get_valuation 估值数据测试"""

    @patch("stock_model.data.sources.baostock_source.bs")
    def test_get_valuation_success(self, mock_bs):
        mock_bs.login.return_value = _mock_login_result()
        val_data = [["2024-01-01", "10.5", "1.2", "2.3", "5.0"]]
        mock_bs.query_history_k_data_plus.return_value = _mock_query_result(
            data=val_data, fields=["date", "peTTM", "pbMRQ", "psTTM", "pcfNcfTTM"]
        )
        BaostockSource._logged_in = False
        src = BaostockSource()
        df = src.get_valuation("000001")
        assert "symbol" in df.columns
        assert df["symbol"].iloc[0] == "000001"
        BaostockSource._logged_in = False

    @patch("stock_model.data.sources.baostock_source.bs")
    def test_get_valuation_no_data(self, mock_bs):
        mock_bs.login.return_value = _mock_login_result()
        mock_bs.query_history_k_data_plus.return_value = _mock_query_result(
            data=[], fields=["date", "peTTM", "pbMRQ", "psTTM", "pcfNcfTTM"]
        )
        BaostockSource._logged_in = False
        src = BaostockSource()
        df = src.get_valuation("000001")
        assert df.empty
        BaostockSource._logged_in = False


class TestGetFinancialSummary:
    """get_financial_summary 财务摘要测试"""

    @patch("stock_model.data.sources.baostock_source.bs")
    def test_financial_summary_success(self, mock_bs):
        mock_bs.login.return_value = _mock_login_result()
        profit_data = [["2024", "4", "sz.000001", "0.15", "0.30", "0.40", "1000000", "1.5"]]
        mock_bs.query_profit_data.return_value = _mock_query_result(
            data=profit_data,
            fields=[
                "year",
                "quarter",
                "code",
                "roeAvg",
                "npMargin",
                "gpMargin",
                "netProfit",
                "epsTTM",
            ],
        )
        BaostockSource._logged_in = False
        src = BaostockSource()
        df = src.get_financial_summary("000001")
        assert "symbol" in df.columns
        BaostockSource._logged_in = False

    @patch("stock_model.data.sources.baostock_source.bs")
    def test_financial_summary_fallback_year(self, mock_bs):
        """默认年份数据为空时回退到前一年"""
        mock_bs.login.return_value = _mock_login_result()
        # 第一次查询返回空，第二次返回数据
        empty_rs = _mock_query_result(data=[])
        profit_data = [["2023", "4", "sz.000001", "0.12", "0.25", "0.35", "800000", "1.2"]]
        fallback_rs = _mock_query_result(
            data=profit_data,
            fields=[
                "year",
                "quarter",
                "code",
                "roeAvg",
                "npMargin",
                "gpMargin",
                "netProfit",
                "epsTTM",
            ],
        )
        mock_bs.query_profit_data.side_effect = [empty_rs, fallback_rs]
        BaostockSource._logged_in = False
        src = BaostockSource()
        df = src.get_financial_summary("000001")
        assert "symbol" in df.columns
        BaostockSource._logged_in = False

    @patch("stock_model.data.sources.baostock_source.bs")
    def test_financial_summary_both_years_empty(self, mock_bs):
        """两年数据都为空"""
        mock_bs.login.return_value = _mock_login_result()
        mock_bs.query_profit_data.return_value = _mock_query_result(data=[])
        BaostockSource._logged_in = False
        src = BaostockSource()
        df = src.get_financial_summary("000001")
        assert df.empty
        BaostockSource._logged_in = False


class TestNormalize:
    """_normalize 标准化测试"""

    def test_normalize_kline_data(self):
        df = pd.DataFrame(
            {
                "date": ["2024-01-01", "2024-01-02"],
                "open": ["10.0", "11.0"],
                "high": ["11.0", "12.0"],
                "low": ["9.0", "10.0"],
                "close": ["10.5", "11.5"],
                "volume": ["1000", "2000"],
                "amount": ["10000", "20000"],
                "turn": ["1.5", "2.0"],
                "pctChg": ["0.5", "1.0"],
            }
        )
        result = BaostockSource._normalize(df, "000001")
        assert "symbol" in result.columns
        assert result["symbol"].iloc[0] == "000001"
        assert result.index.name == "date"
        # 数值类型转换
        assert result["open"].dtype in ["float64", "Int64"]

    def test_normalize_partial_columns(self):
        """只有部分列时也能标准化"""
        df = pd.DataFrame(
            {
                "date": ["2024-01-01"],
                "open": ["10.0"],
                "close": ["10.5"],
            }
        )
        result = BaostockSource._normalize(df, "600000")
        assert "symbol" in result.columns
        assert "open" in result.columns
        assert "close" in result.columns
