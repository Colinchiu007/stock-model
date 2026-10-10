"""AkshareSource 数据源测试"""

from unittest.mock import patch

import pandas as pd
import pytest

from stock_model.data.sources.akshare_source import AkshareSource


def _make_daily_df(n: int = 10) -> pd.DataFrame:
    """构造模拟akshare日线返回的DataFrame(中文列名)"""
    return pd.DataFrame(
        {
            "日期": pd.date_range("2024-01-01", periods=n, freq="B").strftime("%Y-%m-%d"),
            "开盘": range(n),
            "收盘": range(n),
            "最高": range(n),
            "最低": range(n),
            "成交量": range(n),
            "成交额": range(n),
            "振幅": [0.1] * n,
            "涨跌幅": [0.01] * n,
            "涨跌额": [0.1] * n,
            "换手率": [0.5] * n,
        }
    )


def _make_valuation_df(n: int = 5) -> pd.DataFrame:
    """构造模拟估值数据"""
    return pd.DataFrame(
        {
            "trade_date": pd.date_range("2024-01-01", periods=n, freq="B"),
            "pe": [10.0 + i for i in range(n)],
            "pe_ttm": [11.0 + i for i in range(n)],
            "pb": [1.0 + i * 0.1 for i in range(n)],
            "ps": [2.0 + i * 0.1 for i in range(n)],
            "ps_ttm": [2.5 + i * 0.1 for i in range(n)],
            "dv_ratio": [0.03] * n,
            "dv_ttm": [0.04] * n,
            "total_mv": [100000 + i for i in range(n)],
        }
    )


class TestAkshareSourceInit:
    """AkshareSource 初始化测试"""

    def test_name(self):
        assert AkshareSource.name == "akshare"

    def test_is_datasource(self):
        from stock_model.data.sources.base import DataSource

        assert isinstance(AkshareSource(), DataSource)


class TestGetDaily:
    """get_daily 日线数据测试"""

    @patch("stock_model.data.sources.akshare_source.ak")
    def test_get_daily_success(self, mock_ak):
        mock_ak.stock_zh_a_hist.return_value = _make_daily_df()
        src = AkshareSource()
        df = src.get_daily("000001")

        mock_ak.stock_zh_a_hist.assert_called_once_with(
            symbol="000001", period="daily", start_date=None, end_date=None, adjust="qfq"
        )
        assert "symbol" in df.columns
        assert df["symbol"].iloc[0] == "000001"
        assert df.index.name == "date"

    @patch("stock_model.data.sources.akshare_source.ak")
    def test_get_daily_with_dates(self, mock_ak):
        mock_ak.stock_zh_a_hist.return_value = _make_daily_df()
        src = AkshareSource()
        df = src.get_daily("600000", start_date="20240101", end_date="20241231", adjust="hfq")

        mock_ak.stock_zh_a_hist.assert_called_once_with(
            symbol="600000",
            period="daily",
            start_date="20240101",
            end_date="20241231",
            adjust="hfq",
        )
        assert len(df) > 0


class TestGetWeekly:
    """get_weekly 周线数据测试"""

    @patch("stock_model.data.sources.akshare_source.ak")
    def test_get_weekly_success(self, mock_ak):
        mock_ak.stock_zh_a_hist.return_value = _make_daily_df()
        src = AkshareSource()
        df = src.get_weekly("000001")

        mock_ak.stock_zh_a_hist.assert_called_once_with(
            symbol="000001", period="weekly", start_date=None, end_date=None, adjust="qfq"
        )
        assert len(df) > 0


class TestGetMonthly:
    """get_monthly 月线数据测试"""

    @patch("stock_model.data.sources.akshare_source.ak")
    def test_get_monthly_success(self, mock_ak):
        mock_ak.stock_zh_a_hist.return_value = _make_daily_df()
        src = AkshareSource()
        df = src.get_monthly("000001")

        mock_ak.stock_zh_a_hist.assert_called_once_with(
            symbol="000001", period="monthly", start_date=None, end_date=None, adjust="qfq"
        )
        assert len(df) > 0


class TestGetRealtime:
    """get_realtime 实时行情测试"""

    @patch("stock_model.data.sources.akshare_source.ak")
    def test_get_realtime_success(self, mock_ak):
        mock_df = pd.DataFrame({"代码": ["000001", "600000"], "名称": ["平安", "浦发"]})
        mock_ak.stock_zh_a_spot_em.return_value = mock_df
        src = AkshareSource()
        df = src.get_realtime("000001")

        assert len(df) == 1
        assert df.iloc[0]["代码"] == "000001"


class TestGetStockInfo:
    """get_stock_info 股票信息测试"""

    @patch("stock_model.data.sources.akshare_source.ak")
    def test_get_stock_info_success(self, mock_ak):
        mock_ak.stock_individual_info_em.return_value = pd.DataFrame(
            {"item": ["总市值"], "value": ["100亿"]}
        )
        src = AkshareSource()
        df = src.get_stock_info("000001")
        mock_ak.stock_individual_info_em.assert_called_once_with(symbol="000001")
        assert len(df) > 0


class TestGetValuation:
    """get_valuation 估值数据测试"""

    @patch("stock_model.data.sources.akshare_source.ak")
    def test_get_valuation_success(self, mock_ak):
        mock_ak.stock_a_indicator_lg.return_value = _make_valuation_df()
        src = AkshareSource()
        df = src.get_valuation("000001")

        assert "symbol" in df.columns
        assert df["symbol"].iloc[0] == "000001"
        # date应被设为index
        assert df.index.name == "date"


class TestGetFinancialSummary:
    """get_financial_summary 财务摘要测试"""

    @patch("stock_model.data.sources.akshare_source.ak")
    def test_financial_summary_success(self, mock_ak):
        mock_ak.stock_financial_abstract_ths.return_value = pd.DataFrame(
            {"指标": ["营收"], "值": ["100亿"]}
        )
        src = AkshareSource()
        df = src.get_financial_summary("000001")
        assert "symbol" in df.columns

    @patch("stock_model.data.sources.akshare_source.ak")
    def test_financial_summary_primary_fails_fallback_succeeds(self, mock_ak):
        """主接口失败，备用接口成功"""
        mock_ak.stock_financial_abstract_ths.side_effect = ConnectionError("timeout")
        mock_ak.stock_financial_analysis_indicator.return_value = pd.DataFrame(
            {"指标": ["营收"], "值": ["50亿"]}
        )
        src = AkshareSource()
        df = src.get_financial_summary("000001")
        assert "symbol" in df.columns

    @patch("stock_model.data.sources.akshare_source.ak")
    def test_financial_summary_both_fail(self, mock_ak):
        """主接口和备用接口都失败"""
        mock_ak.stock_financial_abstract_ths.side_effect = ConnectionError("timeout")
        mock_ak.stock_financial_analysis_indicator.side_effect = ConnectionError("timeout2")
        src = AkshareSource()
        df = src.get_financial_summary("000001")
        assert df.empty

    @patch("stock_model.data.sources.akshare_source.ak")
    def test_financial_summary_unexpected_error_raises(self, mock_ak):
        """非网络异常直接抛出"""
        mock_ak.stock_financial_abstract_ths.side_effect = ValueError("bad data")
        src = AkshareSource()
        with pytest.raises(ValueError, match="bad data"):
            src.get_financial_summary("000001")


class TestGetSectorList:
    """get_sector_list 行业板块测试"""

    @patch("stock_model.data.sources.akshare_source.ak")
    def test_get_sector_list_success(self, mock_ak):
        mock_ak.stock_board_industry_name_em.return_value = pd.DataFrame(
            {"板块名称": ["银行", "地产"]}
        )
        src = AkshareSource()
        df = src.get_sector_list()
        assert len(df) == 2


class TestGetSectorStocks:
    """get_sector_stocks 板块成分股测试"""

    @patch("stock_model.data.sources.akshare_source.ak")
    def test_get_sector_stocks_success(self, mock_ak):
        mock_ak.stock_board_industry_cons_em.return_value = pd.DataFrame(
            {"代码": ["000001"], "名称": ["平安"]}
        )
        src = AkshareSource()
        src.get_sector_stocks("银行")
        mock_ak.stock_board_industry_cons_em.assert_called_once_with(symbol="银行")


class TestNormalize:
    """_normalize 标准化测试"""

    def test_normalize_chinese_columns(self):
        df = _make_daily_df()
        result = AkshareSource._normalize(df, "000001")

        assert "open" in result.columns
        assert "close" in result.columns
        assert "volume" in result.columns
        assert "symbol" in result.columns
        assert result["symbol"].iloc[0] == "000001"
        # date被设为index
        assert result.index.name == "date"

    def test_normalize_date_as_index(self):
        df = _make_daily_df()
        result = AkshareSource._normalize(df, "600000")
        assert result.index.name == "date"

    def test_normalize_no_date_column(self):
        df = pd.DataFrame({"开盘": [1], "收盘": [2], "最高": [3], "最低": [4], "成交量": [5]})
        result = AkshareSource._normalize(df, "000001")
        assert "symbol" in result.columns
        assert "date" not in result.columns
