"""
图表可视化模块

功能:
  - K线图
  - 技术指标图
  - 综合分析图
  - 信号标注图
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import plotly.graph_objects as go
from loguru import logger
from plotly.subplots import make_subplots

from stock_model.config.settings import get_settings

if TYPE_CHECKING:
    import pandas as pd


class ChartBuilder:
    """图表构建器"""

    def __init__(self):
        self.settings = get_settings().visualization

    def kline(
        self,
        df: pd.DataFrame,
        symbol: str = "",
        title: str | None = None,
        show_volume: bool = True,
        show_ma: bool = True,
        ma_periods: list[int] | None = None,
    ) -> go.Figure:
        """
        绘制K线图

        Args:
            df: 行情数据 (需要 open/high/low/close/volume 列)
            symbol: 股票代码
            title: 图表标题
            show_volume: 是否显示成交量
            show_ma: 是否显示均线
            ma_periods: 均线周期

        Returns:
            Plotly Figure
        """
        title = title or f"{symbol} K线图"
        if ma_periods is None:
            ma_periods = [5, 20, 60]

        # 创建子图
        rows = 2 if show_volume else 1
        fig = make_subplots(
            rows=rows,
            cols=1,
            shared_xaxes=True,
            vertical_spacing=0.05,
            row_heights=[0.7, 0.3] if show_volume else [1.0],
        )

        # K线图
        fig.add_trace(
            go.Candlestick(
                x=df.index,
                open=df["open"],
                high=df["high"],
                low=df["low"],
                close=df["close"],
                name="K线",
            ),
            row=1,
            col=1,
        )

        # 均线
        if show_ma:
            for period in ma_periods:
                col_name = f"ma{period}"
                if col_name in df.columns:
                    fig.add_trace(
                        go.Scatter(
                            x=df.index,
                            y=df[col_name],
                            name=f"MA{period}",
                            line={"width": 1},
                        ),
                        row=1,
                        col=1,
                    )
                else:
                    # 动态计算均线
                    ma_values = df["close"].rolling(window=period).mean()
                    fig.add_trace(
                        go.Scatter(
                            x=df.index,
                            y=ma_values,
                            name=f"MA{period}",
                            line={"width": 1},
                        ),
                        row=1,
                        col=1,
                    )

        # 成交量
        if show_volume and "volume" in df.columns:
            colors = [
                "red" if close >= open_ else "green"
                for close, open_ in zip(df["close"], df["open"], strict=False)
            ]
            fig.add_trace(
                go.Bar(
                    x=df.index,
                    y=df["volume"],
                    name="成交量",
                    marker_color=colors,
                    opacity=0.7,
                ),
                row=2,
                col=1,
            )

        # 更新布局
        fig.update_layout(
            title=title,
            xaxis_rangeslider_visible=False,
            template="plotly_dark" if self.settings.theme == "dark" else "plotly_white",
            width=self.settings.width,
            height=self.settings.height,
        )

        return fig

    def technical_indicators(
        self,
        df: pd.DataFrame,
        symbol: str = "",
        indicators: list[str] | None = None,
    ) -> go.Figure:
        """
        绘制技术指标图

        Args:
            df: 包含技术指标的行情数据
            symbol: 股票代码
            indicators: 需要显示的指标

        Returns:
            Plotly Figure
        """
        if indicators is None:
            indicators = ["macd", "rsi", "boll"]
        n_indicators = len(indicators)
        # K线 + 指标子图
        fig = make_subplots(
            rows=1 + n_indicators,
            cols=1,
            shared_xaxes=True,
            vertical_spacing=0.05,
        )

        # K线
        fig.add_trace(
            go.Candlestick(
                x=df.index,
                open=df["open"],
                high=df["high"],
                low=df["low"],
                close=df["close"],
                name="K线",
            ),
            row=1,
            col=1,
        )

        for i, indicator in enumerate(indicators, start=2):
            if indicator == "macd":
                self._add_macd_trace(fig, df, row=i)
            elif indicator == "rsi":
                self._add_rsi_trace(fig, df, row=i)
            elif indicator == "boll":
                self._add_boll_trace(fig, df, row=1)  # BOLL叠加在K线上

        fig.update_layout(
            title=f"{symbol} 技术分析",
            xaxis_rangeslider_visible=False,
            template="plotly_dark" if self.settings.theme == "dark" else "plotly_white",
            width=self.settings.width,
            height=self.settings.height * 1.5,
        )

        return fig

    def _add_macd_trace(self, fig: go.Figure, df: pd.DataFrame, row: int) -> None:
        """添加MACD指标"""
        macd_col = signal_col = hist_col = None
        for col in df.columns:
            if "MACD_" in col and "MACDs" not in col and "MACDh" not in col:
                macd_col = col
            elif "MACDs_" in col:
                signal_col = col
            elif "MACDh_" in col:
                hist_col = col

        if macd_col:
            fig.add_trace(go.Scatter(x=df.index, y=df[macd_col], name="MACD"), row=row, col=1)
        if signal_col:
            fig.add_trace(go.Scatter(x=df.index, y=df[signal_col], name="Signal"), row=row, col=1)
        if hist_col:
            colors = ["red" if v >= 0 else "green" for v in df[hist_col]]
            fig.add_trace(
                go.Bar(x=df.index, y=df[hist_col], name="Histogram", marker_color=colors),
                row=row,
                col=1,
            )

    def _add_rsi_trace(self, fig: go.Figure, df: pd.DataFrame, row: int) -> None:
        """添加RSI指标"""
        rsi_col = None
        for col in df.columns:
            if "RSI" in col.upper():
                rsi_col = col
                break

        if rsi_col:
            fig.add_trace(go.Scatter(x=df.index, y=df[rsi_col], name="RSI"), row=row, col=1)
            # 超买超卖线
            fig.add_hline(y=70, line_dash="dash", line_color="red", row=row, col=0)
            fig.add_hline(y=30, line_dash="dash", line_color="green", row=row, col=0)

    def _add_boll_trace(self, fig: go.Figure, df: pd.DataFrame, row: int) -> None:
        """添加布林带"""
        lower_col = upper_col = None
        for col in df.columns:
            if "BBL_" in col:
                lower_col = col
            elif "BBM_" in col:
                pass
            elif "BBU_" in col:
                upper_col = col

        if upper_col and lower_col:
            fig.add_trace(
                go.Scatter(x=df.index, y=df[upper_col], name="BOLL Upper", line={"width": 1}),
                row=row,
                col=1,
            )
            fig.add_trace(
                go.Scatter(x=df.index, y=df[lower_col], name="BOLL Lower", line={"width": 1}),
                row=row,
                col=1,
            )
            # 填充区域
            fig.add_trace(
                go.Scatter(
                    x=df.index,
                    y=df[upper_col],
                    fill=None,
                    name="BOLL Upper Fill",
                    showlegend=False,
                    line={"width": 0},
                ),
                row=row,
                col=1,
            )
            fig.add_trace(
                go.Scatter(
                    x=df.index,
                    y=df[lower_col],
                    fill="tonexty",
                    name="BOLL Band",
                    showlegend=False,
                    line={"width": 0},
                    fillcolor="rgba(128,128,128,0.2)",
                ),
                row=row,
                col=1,
            )

    def export(
        self,
        fig: go.Figure,
        filepath: str | Path,
        format: str | None = None,
    ) -> Path:
        """
        导出图表

        Args:
            fig: Plotly Figure
            filepath: 导出路径
            format: 导出格式 html/png/svg/pdf

        Returns:
            导出文件路径
        """
        format = format or self.settings.export_format
        filepath = Path(filepath)

        if format == "html":
            fig.write_html(str(filepath))
        elif format == "png":
            fig.write_image(str(filepath), format="png")
        elif format == "svg":
            fig.write_image(str(filepath), format="svg")
        elif format == "pdf":
            fig.write_image(str(filepath), format="pdf")
        else:
            raise ValueError(f"不支持的导出格式: {format}")

        logger.info(f"图表已导出: {filepath}")
        return filepath
