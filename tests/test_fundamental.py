"""基本面分析模块测试

覆盖 FundamentalScore、FundamentalAnalysis 的评分逻辑和综合分析。
"""

from unittest.mock import MagicMock, patch

import pandas as pd

from stock_model.analysis.fundamental import FundamentalAnalysis, FundamentalScore


class TestFundamentalScore:
    """FundamentalScore 数据类测试"""

    def test_default_values(self):
        """默认值"""
        score = FundamentalScore(symbol="000001")
        assert score.symbol == "000001"
        assert score.name == ""
        assert score.pe_score == 0.0
        assert score.pb_score == 0.0
        assert score.total_score == 0.0
        assert score.raw_data == {}

    def test_str_representation(self):
        """字符串表示"""
        score = FundamentalScore(
            symbol="000001",
            pe_score=80.0,
            pb_score=70.0,
            roe_score=60.0,
            growth_score=50.0,
            total_score=65.0,
        )
        s = str(score)
        assert "000001" in s
        assert "PE=80.0" in s
        assert "Total=65.0" in s

    def test_custom_values(self):
        """自定义值"""
        score = FundamentalScore(
            symbol="600000",
            name="浦发银行",
            pe_score=75.0,
            pb_score=65.0,
            ps_score=55.0,
            roe_score=80.0,
            profit_score=70.0,
            growth_score=60.0,
            total_score=70.0,
            raw_data={"pe": 12.5},
        )
        assert score.name == "浦发银行"
        assert score.ps_score == 55.0
        assert score.raw_data["pe"] == 12.5


class TestScorePE:
    """PE评分测试"""

    def test_negative_pe(self):
        """负PE(亏损)应得0分"""
        fa = FundamentalAnalysis.__new__(FundamentalAnalysis)
        assert fa.score_pe(-5.0) == 0.0

    def test_zero_pe(self):
        """零PE应得0分"""
        fa = FundamentalAnalysis.__new__(FundamentalAnalysis)
        assert fa.score_pe(0.0) == 0.0

    def test_low_pe(self):
        """低PE(0-15)应得80-100分"""
        fa = FundamentalAnalysis.__new__(FundamentalAnalysis)
        score = fa.score_pe(10.0)
        assert 80.0 <= score <= 100.0

    def test_moderate_pe(self):
        """合理PE(15-30)应得60-80分"""
        fa = FundamentalAnalysis.__new__(FundamentalAnalysis)
        score = fa.score_pe(20.0)
        assert 60.0 <= score <= 80.0

    def test_high_pe(self):
        """偏高PE(30-50)应得30-60分"""
        fa = FundamentalAnalysis.__new__(FundamentalAnalysis)
        score = fa.score_pe(40.0)
        assert 30.0 <= score <= 60.0

    def test_very_high_pe(self):
        """高估PE(50+)应得0-30分"""
        fa = FundamentalAnalysis.__new__(FundamentalAnalysis)
        score = fa.score_pe(100.0)
        assert 0.0 <= score <= 30.0


class TestScorePB:
    """PB评分测试"""

    def test_negative_pb(self):
        """负PB应得0分"""
        fa = FundamentalAnalysis.__new__(FundamentalAnalysis)
        assert fa.score_pb(-1.0) == 0.0

    def test_low_pb(self):
        """低PB(0-1)应得80-100分"""
        fa = FundamentalAnalysis.__new__(FundamentalAnalysis)
        score = fa.score_pb(0.5)
        assert 80.0 <= score <= 100.0

    def test_moderate_pb(self):
        """合理PB(1-3)应得50-80分"""
        fa = FundamentalAnalysis.__new__(FundamentalAnalysis)
        score = fa.score_pb(2.0)
        assert 50.0 <= score <= 80.0

    def test_high_pb(self):
        """偏高PB(3-5)应得20-50分"""
        fa = FundamentalAnalysis.__new__(FundamentalAnalysis)
        score = fa.score_pb(4.0)
        assert 20.0 <= score <= 50.0

    def test_very_high_pb(self):
        """高估PB(5+)应得0-20分"""
        fa = FundamentalAnalysis.__new__(FundamentalAnalysis)
        score = fa.score_pb(10.0)
        assert 0.0 <= score <= 20.0


class TestScoreROE:
    """ROE评分测试"""

    def test_negative_roe(self):
        """负ROE应得0分"""
        fa = FundamentalAnalysis.__new__(FundamentalAnalysis)
        assert fa.score_roe(-5.0) == 0.0

    def test_low_roe(self):
        """低ROE(0-5)应得0-20分"""
        fa = FundamentalAnalysis.__new__(FundamentalAnalysis)
        score = fa.score_roe(3.0)
        assert 0.0 <= score <= 20.0

    def test_moderate_roe(self):
        """合理ROE(10-15)应得40-60分"""
        fa = FundamentalAnalysis.__new__(FundamentalAnalysis)
        score = fa.score_roe(12.0)
        assert 40.0 <= score <= 60.0

    def test_high_roe(self):
        """高ROE(20+)应得80-100分"""
        fa = FundamentalAnalysis.__new__(FundamentalAnalysis)
        score = fa.score_roe(25.0)
        assert 80.0 <= score <= 100.0


class TestFundamentalAnalysisInit:
    """FundamentalAnalysis初始化测试"""

    def test_init_with_fetcher(self):
        """使用自定义fetcher初始化"""
        mock_fetcher = MagicMock()
        fa = FundamentalAnalysis(fetcher=mock_fetcher)
        assert fa.fetcher is mock_fetcher

    @patch("stock_model.analysis.fundamental.StockDataFetcher")
    def test_init_default_fetcher(self, MockFetcher):
        """默认fetcher初始化"""
        fa = FundamentalAnalysis()
        assert fa.fetcher is not None


class TestAnalyze:
    """综合分析测试"""

    @patch("stock_model.analysis.fundamental.StockDataFetcher")
    def test_analyze_with_valuation(self, MockFetcher):
        """有估值数据的分析"""
        mock_fetcher = MagicMock()
        valuation_df = pd.DataFrame(
            {"pe": [15.0], "pb": [2.0]},
            index=["latest"],
        )
        mock_fetcher.get_valuation.return_value = valuation_df

        fa = FundamentalAnalysis(fetcher=mock_fetcher)
        score = fa.analyze("000001")

        assert score.symbol == "000001"
        assert score.pe_score > 0
        assert score.pb_score > 0
        assert score.total_score > 0

    @patch("stock_model.analysis.fundamental.StockDataFetcher")
    def test_analyze_empty_valuation(self, MockFetcher):
        """空估值数据"""
        mock_fetcher = MagicMock()
        mock_fetcher.get_valuation.return_value = pd.DataFrame()

        fa = FundamentalAnalysis(fetcher=mock_fetcher)
        score = fa.analyze("000001")

        assert score.symbol == "000001"
        assert score.pe_score == 0.0
        assert score.pb_score == 0.0

    @patch("stock_model.analysis.fundamental.StockDataFetcher")
    def test_analyze_valuation_error(self, MockFetcher):
        """估值数据获取失败"""
        mock_fetcher = MagicMock()
        mock_fetcher.get_valuation.side_effect = ConnectionError("网络错误")

        fa = FundamentalAnalysis(fetcher=mock_fetcher)
        score = fa.analyze("000001")

        assert score.symbol == "000001"
        assert score.pe_score == 0.0


class TestBatchAnalyze:
    """批量分析测试"""

    @patch("stock_model.analysis.fundamental.StockDataFetcher")
    def test_batch_analyze(self, MockFetcher):
        """批量分析多个股票"""
        mock_fetcher = MagicMock()
        mock_fetcher.get_valuation.return_value = pd.DataFrame()

        fa = FundamentalAnalysis(fetcher=mock_fetcher)
        results = fa.batch_analyze(["000001", "600000"])

        assert len(results) == 2
        assert all(isinstance(r, FundamentalScore) for r in results)

    @patch("stock_model.analysis.fundamental.StockDataFetcher")
    def test_batch_analyze_sorted(self, MockFetcher):
        """批量分析结果按评分排序"""
        mock_fetcher = MagicMock()

        call_count = 0

        def mock_get_valuation(symbol):
            nonlocal call_count
            call_count += 1
            if symbol == "000001":
                return pd.DataFrame({"pe": [10.0], "pb": [0.5]}, index=["latest"])
            return pd.DataFrame({"pe": [50.0], "pb": [5.0]}, index=["latest"])

        mock_fetcher.get_valuation.side_effect = mock_get_valuation

        fa = FundamentalAnalysis(fetcher=mock_fetcher)
        results = fa.batch_analyze(["000001", "600000"])

        assert results[0].total_score >= results[1].total_score
