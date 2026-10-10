"""DataProcessor 测试

覆盖数据清洗、收益率计算、多股票对齐、重采样、标准化。
"""

import numpy as np
import pandas as pd
import pytest

from stock_model.data.processor import DataProcessor


@pytest.fixture
def sample_df():
    """构造测试用行情数据"""
    dates = pd.date_range("2024-01-01", periods=20, freq="D")
    return pd.DataFrame(
        {
            "open": np.random.uniform(10, 20, 20),
            "high": np.random.uniform(15, 25, 20),
            "low": np.random.uniform(5, 15, 20),
            "close": np.random.uniform(10, 20, 20),
            "volume": np.random.randint(1000, 10000, 20),
            "amount": np.random.uniform(10000, 50000, 20),
        },
        index=dates,
    )


class TestClean:
    """数据清洗测试"""

    def test_clean_basic(self, sample_df):
        """基本清洗不报错"""
        result = DataProcessor.clean(sample_df)
        assert isinstance(result, pd.DataFrame)
        assert len(result) <= len(sample_df)

    def test_clean_remove_nan_true(self):
        """remove_nan=True 移除NaN行"""
        df = pd.DataFrame(
            {"close": [10.0, np.nan, 20.0], "volume": [1000, 2000, 0]},
            index=pd.date_range("2024-01-01", periods=3),
        )
        result = DataProcessor.clean(df, remove_nan=True)
        # volume=0 被替换为NaN后dropna移除
        assert result.isna().sum().sum() == 0

    def test_clean_remove_nan_false(self):
        """remove_nan=False 保留NaN行"""
        df = pd.DataFrame(
            {"close": [10.0, np.nan, 20.0], "volume": [1000, 2000, 3000]},
            index=pd.date_range("2024-01-01", periods=3),
        )
        result = DataProcessor.clean(df, remove_nan=False)
        assert len(result) == 3

    def test_clean_volume_zero_to_nan(self):
        """volume为0替换为NaN"""
        df = pd.DataFrame(
            {"close": [10.0, 20.0], "volume": [0, 1000]},
            index=pd.date_range("2024-01-01", periods=2),
        )
        result = DataProcessor.clean(df, remove_nan=False)
        assert np.isnan(result["volume"].iloc[0])

    def test_clean_preserves_original(self, sample_df):
        """清洗不修改原始数据"""
        original_len = len(sample_df)
        DataProcessor.clean(sample_df)
        assert len(sample_df) == original_len


class TestCalculateReturns:
    """收益率计算测试"""

    def test_calculate_returns_basic(self, sample_df):
        """基本收益率计算"""
        result = DataProcessor.calculate_returns(sample_df)
        assert "return" in result.columns
        assert "log_return" in result.columns
        assert "cum_return" in result.columns

    def test_calculate_returns_first_row_nan(self):
        """第一行收益率为NaN"""
        df = pd.DataFrame(
            {"close": [10.0, 11.0, 12.0]},
            index=pd.date_range("2024-01-01", periods=3),
        )
        result = DataProcessor.calculate_returns(df)
        assert np.isnan(result["return"].iloc[0])

    def test_calculate_returns_custom_column(self):
        """自定义列计算收益率"""
        df = pd.DataFrame(
            {"price": [100.0, 110.0, 121.0]},
            index=pd.date_range("2024-01-01", periods=3),
        )
        result = DataProcessor.calculate_returns(df, column="price")
        assert "return" in result.columns
        assert result["return"].iloc[1] == pytest.approx(0.1, abs=0.01)

    def test_calculate_returns_preserves_original(self, sample_df):
        """计算不修改原始数据"""
        original_cols = set(sample_df.columns)
        DataProcessor.calculate_returns(sample_df)
        assert set(sample_df.columns) == original_cols


class TestAlignMultiple:
    """多股票对齐测试"""

    def test_align_empty_dict(self):
        """空字典返回空"""
        result = DataProcessor.align_multiple({})
        assert result == {}

    def test_align_inner(self):
        """inner对齐取交集"""
        dates1 = pd.date_range("2024-01-01", periods=5)
        dates2 = pd.date_range("2024-01-03", periods=5)
        df1 = pd.DataFrame({"close": range(5)}, index=dates1)
        df2 = pd.DataFrame({"close": range(5)}, index=dates2)
        result = DataProcessor.align_multiple({"A": df1, "B": df2}, method="inner")
        # 交集: 2024-01-03 ~ 2024-01-05
        assert len(result["A"]) == 3
        assert len(result["B"]) == 3

    def test_align_outer(self):
        """outer对齐取并集"""
        dates1 = pd.date_range("2024-01-01", periods=3)
        dates2 = pd.date_range("2024-01-02", periods=3)
        df1 = pd.DataFrame({"close": range(3)}, index=dates1)
        df2 = pd.DataFrame({"close": range(3)}, index=dates2)
        result = DataProcessor.align_multiple({"A": df1, "B": df2}, method="outer")
        # outer: 并集4个日期，但各df只保留自己有的行
        # df1有01-01/02/03，df2有02/03/04
        assert len(result["A"]) == 3  # df1原有3行
        assert len(result["B"]) == 3  # df2原有3行


class TestResample:
    """重采样测试"""

    def test_resample_weekly(self, sample_df):
        """周频重采样"""
        result = DataProcessor.resample(sample_df, freq="W")
        assert isinstance(result, pd.DataFrame)
        assert len(result) < len(sample_df)

    def test_resample_monthly(self):
        """月频重采样"""
        dates = pd.date_range("2024-01-01", periods=60, freq="D")
        df = pd.DataFrame(
            {
                "open": np.random.uniform(10, 20, 60),
                "high": np.random.uniform(15, 25, 60),
                "low": np.random.uniform(5, 15, 60),
                "close": np.random.uniform(10, 20, 60),
                "volume": np.random.randint(1000, 10000, 60),
            },
            index=dates,
        )
        result = DataProcessor.resample(df, freq="ME")
        assert isinstance(result, pd.DataFrame)
        assert len(result) < len(df)

    def test_resample_preserves_original(self, sample_df):
        """重采样不修改原始数据"""
        original_len = len(sample_df)
        DataProcessor.resample(sample_df)
        assert len(sample_df) == original_len


class TestNormalize:
    """标准化测试"""

    def test_normalize_basic(self, sample_df):
        """基本标准化"""
        result = DataProcessor.normalize(sample_df)
        assert isinstance(result, pd.DataFrame)
        # 应产生_norm列
        norm_cols = [c for c in result.columns if c.endswith("_norm")]
        assert len(norm_cols) > 0

    def test_normalize_specific_columns(self):
        """指定列标准化"""
        df = pd.DataFrame(
            {"close": [10.0, 20.0, 30.0], "volume": [1000, 2000, 3000]},
            index=pd.date_range("2024-01-01", periods=3),
        )
        result = DataProcessor.normalize(df, columns=["close"])
        assert "close_norm" in result.columns
        assert "volume_norm" not in result.columns

    def test_normalize_zero_std(self):
        """标准差为0时不产生norm列"""
        df = pd.DataFrame(
            {"close": [10.0, 10.0, 10.0]},
            index=pd.date_range("2024-01-01", periods=3),
        )
        result = DataProcessor.normalize(df, columns=["close"])
        # std=0, 不应产生close_norm
        assert "close_norm" not in result.columns

    def test_normalize_preserves_original(self, sample_df):
        """标准化不修改原始数据"""
        original_cols = set(sample_df.columns)
        DataProcessor.normalize(sample_df)
        assert set(sample_df.columns) == original_cols
