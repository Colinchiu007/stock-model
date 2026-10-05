"""ChartBuilder 可视化模块测试

覆盖 K线图、技术指标图、布林带、导出功能。
"""

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import pytest

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

from stock_model.visualization.charts import ChartBuilder  # noqa: E402


@pytest.fixture
def sample_df():
    """创建测试用行情数据"""
    np.random.seed(42)
    dates = pd.date_range("2024-01-01", periods=60, freq="B")
    close = 15.0 + np.cumsum(np.random.randn(60) * 0.3)
    close = np.maximum(close, 1.0)
    return pd.DataFrame(
        {
            "open": close * (1 + np.random.randn(60) * 0.01),
            "high": close * (1 + abs(np.random.randn(60) * 0.02)),
            "low": close * (1 - abs(np.random.randn(60) * 0.02)),
            "close": close,
            "volume": np.random.randint(10000, 100000, 60).astype(float),
        },
        index=dates,
    )


@pytest.fixture
def sample_df_with_indicators(sample_df):
    """带技术指标的行情数据"""
    df = sample_df.copy()
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["MACD_12_26_9"] = df["close"].ewm(span=12).mean() - df["close"].ewm(span=26).mean()
    df["MACDs_12_26_9"] = df["MACD_12_26_9"].ewm(span=9).mean()
    df["MACDh_12_26_9"] = df["MACD_12_26_9"] - df["MACDs_12_26_9"]
    df["RSI_14"] = 50 + np.random.randn(60) * 10  # 简化RSI
    df["BBL_5_2.0"] = df["close"].rolling(5).mean() - 2 * df["close"].rolling(5).std()
    df["BBU_5_2.0"] = df["close"].rolling(5).mean() + 2 * df["close"].rolling(5).std()
    return df


@pytest.fixture
def builder():
    """创建ChartBuilder实例"""
    return ChartBuilder()


class TestChartBuilderInit:
    """ChartBuilder初始化测试"""

    def test_init(self, builder):
        """验证初始化成功"""
        assert builder.settings is not None


class TestKline:
    """K线图测试"""

    def test_kline_basic(self, builder, sample_df):
        """基本K线图"""
        fig = builder.kline(sample_df, symbol="000001")
        assert isinstance(fig, go.Figure)
        assert len(fig.data) >= 1  # 至少有K线trace

    def test_kline_with_volume(self, builder, sample_df):
        """K线图含成交量"""
        fig = builder.kline(sample_df, show_volume=True)
        # 应有K线 + 成交量两个trace
        trace_names = [t.name for t in fig.data]
        assert "K线" in trace_names
        assert "成交量" in trace_names

    def test_kline_without_volume(self, builder, sample_df):
        """K线图不含成交量"""
        fig = builder.kline(sample_df, show_volume=False)
        trace_names = [t.name for t in fig.data]
        assert "成交量" not in trace_names

    def test_kline_with_ma_columns(self, builder, sample_df_with_indicators):
        """K线图使用已有MA列"""
        fig = builder.kline(sample_df_with_indicators, show_ma=True, ma_periods=[5, 20])
        trace_names = [t.name for t in fig.data]
        assert "MA5" in trace_names
        assert "MA20" in trace_names

    def test_kline_with_dynamic_ma(self, builder, sample_df):
        """K线图动态计算MA"""
        fig = builder.kline(sample_df, show_ma=True, ma_periods=[5])
        trace_names = [t.name for t in fig.data]
        assert "MA5" in trace_names

    def test_kline_custom_title(self, builder, sample_df):
        """自定义标题"""
        fig = builder.kline(sample_df, symbol="600000", title="自定义标题")
        assert fig.layout.title.text == "自定义标题"

    def test_kline_default_title(self, builder, sample_df):
        """默认标题"""
        fig = builder.kline(sample_df, symbol="000001")
        assert "000001" in fig.layout.title.text


class TestTechnicalIndicators:
    """技术指标图测试"""

    def test_technical_indicators_default(self, builder, sample_df_with_indicators):
        """默认技术指标(macd, rsi, boll)"""
        fig = builder.technical_indicators(sample_df_with_indicators, symbol="000001")
        assert isinstance(fig, go.Figure)
        assert len(fig.data) >= 1

    def test_technical_indicators_custom(self, builder, sample_df_with_indicators):
        """自定义技术指标"""
        fig = builder.technical_indicators(sample_df_with_indicators, indicators=["macd"])
        assert isinstance(fig, go.Figure)

    def test_technical_indicators_rsi(self, builder, sample_df_with_indicators):
        """RSI指标"""
        fig = builder.technical_indicators(sample_df_with_indicators, indicators=["rsi"])
        assert isinstance(fig, go.Figure)

    def test_technical_indicators_boll(self, builder, sample_df_with_indicators):
        """布林带指标"""
        fig = builder.technical_indicators(sample_df_with_indicators, indicators=["boll"])
        assert isinstance(fig, go.Figure)


class TestExport:
    """导出功能测试"""

    def test_export_html(self, builder, sample_df, tmp_path):
        """导出HTML"""
        fig = builder.kline(sample_df)
        filepath = builder.export(fig, tmp_path / "test.html", format="html")
        assert filepath.exists()
        assert filepath.suffix == ".html"

    def test_export_default_format(self, builder, sample_df, tmp_path):
        """默认导出格式"""
        fig = builder.kline(sample_df)
        filepath = builder.export(fig, tmp_path / "test.html")
        assert filepath.exists()

    def test_export_unsupported_format(self, builder, sample_df, tmp_path):
        """不支持的格式应报错"""
        fig = builder.kline(sample_df)
        with pytest.raises(ValueError, match="不支持的导出格式"):
            builder.export(fig, tmp_path / "test.xyz", format="xyz")


class TestModuleImport:
    """模块导入测试"""

    def test_import_chart_builder(self):
        """验证ChartBuilder可导入"""
        from stock_model.visualization import ChartBuilder as CB

        assert CB is ChartBuilder
