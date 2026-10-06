"""
技术分析模块

功能:
  - 移动均线 (MA/SMA/EMA)
  - MACD
  - RSI
  - 布林带 (BOLL)
  - KDJ
  - 成量指标 (OBV/VOL_MA)
  - 综合技术分析
"""

from __future__ import annotations

import pandas as pd
from loguru import logger

try:
    import pandas_ta as ta

    HAS_PANDAS_TA = True
except ImportError:
    ta = None
    HAS_PANDAS_TA = False

from stock_model.analysis import indicators as fallback_indicators
from stock_model.config.settings import get_settings


class TechnicalAnalysis:
    """技术分析器

    MACD / RSI / BOLL / ATR / OBV 的计算策略:
      1. 优先使用 pandas-ta（若已安装）
      2. 缺失时回退到 analysis.indicators 的纯 pandas 实现

    回退是必需的而非可选优化: pandas-ta 仅支持 Python >= 3.12，
    而本项目支持 >= 3.10。若无回退，在 3.10/3.11 上这些指标列
    会静默不产生，导致信号生成器永远读不到值、策略恒返回 hold。
    """

    def __init__(self):
        self.settings = get_settings().analysis
        if not HAS_PANDAS_TA:
            logger.info(
                "pandas-ta 未安装，使用内置纯 pandas 指标实现"
                "(MACD/RSI/BOLL/ATR/OBV 功能完整，仅数值口径可能与看盘软件有细微差异)。"
                "如需 pandas-ta: pip install pandas-ta (需 Python>=3.12)"
            )

    # ==================== 趋势指标 ====================

    def ma(self, df: pd.DataFrame, periods: list[int] | None = None) -> pd.DataFrame:
        """
        计算简单移动均线

        Args:
            df: 行情数据 (需要 close 列)
            periods: 均线周期列表

        Returns:
            添加了MA列的DataFrame
        """
        df = df.copy()
        periods = periods or self.settings.ma_periods
        for period in periods:
            df[f"ma{period}"] = df["close"].rolling(window=period).mean()
        return df

    def ema(self, df: pd.DataFrame, periods: list[int] | None = None) -> pd.DataFrame:
        """计算指数移动均线"""
        df = df.copy()
        periods = periods or self.settings.ma_periods
        for period in periods:
            df[f"ema{period}"] = df["close"].ewm(span=period, adjust=False).mean()
        return df

    def macd(
        self,
        df: pd.DataFrame,
        fast: int | None = None,
        slow: int | None = None,
        signal: int | None = None,
    ) -> pd.DataFrame:
        """
        计算MACD指标

        Args:
            df: 行情数据
            fast: 快线周期
            slow: 慢线周期
            signal: 信号线周期

        Returns:
            添加了MACD列的DataFrame
        """
        df = df.copy()
        fast = fast or self.settings.macd_fast
        slow = slow or self.settings.macd_slow
        signal = signal or self.settings.macd_signal

        if HAS_PANDAS_TA:
            macd_result = ta.macd(df["close"], fast=fast, slow=slow, signal=signal)
            if macd_result is not None:
                df = pd.concat([df, macd_result], axis=1)
        else:
            df = fallback_indicators.macd(df, fast=fast, slow=slow, signal=signal)

        return df

    # ==================== 动量指标 ====================

    def rsi(
        self,
        df: pd.DataFrame,
        period: int | None = None,
    ) -> pd.DataFrame:
        """
        计算RSI指标

        Args:
            df: 行情数据
            period: RSI周期

        Returns:
            添加了RSI列的DataFrame
        """
        df = df.copy()
        period = period or self.settings.rsi_period

        if HAS_PANDAS_TA:
            rsi_result = ta.rsi(df["close"], length=period)
            if rsi_result is not None:
                df[f"rsi{period}"] = rsi_result
        else:
            df = fallback_indicators.rsi(df, period=period)

        return df

    def kdj(
        self,
        df: pd.DataFrame,
        n: int = 9,
        m1: int = 3,
        m2: int = 3,
    ) -> pd.DataFrame:
        """
        计算KDJ指标

        Args:
            df: 行情数据 (需要 high, low, close 列)
            n: RSV周期
            m1: K平滑周期
            m2: D平滑周期

        Returns:
            添加了K, D, J列的DataFrame
        """
        df = df.copy()
        low_min = df["low"].rolling(window=n, min_periods=1).min()
        high_max = df["high"].rolling(window=n, min_periods=1).max()
        rsv = (df["close"] - low_min) / (high_max - low_min) * 100

        df["kdj_k"] = rsv.ewm(com=m1 - 1, adjust=False).mean()
        df["kdj_d"] = df["kdj_k"].ewm(com=m2 - 1, adjust=False).mean()
        df["kdj_j"] = 3 * df["kdj_k"] - 2 * df["kdj_d"]

        return df

    # ==================== 波动指标 ====================

    def boll(
        self,
        df: pd.DataFrame,
        period: int | None = None,
        std_dev: float | None = None,
    ) -> pd.DataFrame:
        """
        计算布林带

        Args:
            df: 行情数据
            period: 均线周期
            std_dev: 标准差倍数

        Returns:
            添加了BOLL列的DataFrame
        """
        df = df.copy()
        period = period or self.settings.boll_period
        std_dev = std_dev or self.settings.boll_std

        if HAS_PANDAS_TA:
            boll_result = ta.bbands(df["close"], length=period, std=std_dev)
            if boll_result is not None:
                df = pd.concat([df, boll_result], axis=1)
        else:
            df = fallback_indicators.bollinger(df, period=period, std_dev=std_dev)

        return df

    def atr(self, df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
        """计算ATR (真实波动范围)"""
        df = df.copy()
        if HAS_PANDAS_TA:
            atr_result = ta.atr(df["high"], df["low"], df["close"], length=period)
            if atr_result is not None:
                df[f"atr{period}"] = atr_result
        else:
            df = fallback_indicators.atr(df, period=period)
            # 与 pandas-ta 的列名保持一致
            df[f"atr{period}"] = df[f"ATR_{period}"]
        return df

    # ==================== 量价指标 ====================

    def obv(self, df: pd.DataFrame) -> pd.DataFrame:
        """计算OBV (能量潮)"""
        df = df.copy()
        if HAS_PANDAS_TA:
            df["obv"] = ta.obv(df["close"], df["volume"])
        else:
            df["obv"] = fallback_indicators.obv(df)["OBV"]
        return df

    def volume_ma(self, df: pd.DataFrame, periods: list[int] | None = None) -> pd.DataFrame:
        """计算成交量均线"""
        if periods is None:
            periods = [5, 10, 20]
        df = df.copy()
        for period in periods:
            df[f"vol_ma{period}"] = df["volume"].rolling(window=period).mean()
        return df

    # ==================== 综合分析 ====================

    def analyze_all(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        执行全部技术分析

        Args:
            df: 厗情数据

        Returns:
            添加了所有技术指标列的DataFrame
        """
        logger.debug(f"执行综合技术分析, 数据量: {len(df)} 条")
        df = df.copy()

        # 趋势指标
        df = self.ma(df)
        df = self.ema(df)
        df = self.macd(df)

        # 动量指标
        df = self.rsi(df)
        df = self.kdj(df)

        # 波动指标
        df = self.boll(df)
        df = self.atr(df)

        # 量价指标
        df = self.obv(df)
        df = self.volume_ma(df)

        logger.debug(f"技术分析完成, 指标列数: {len(df.columns)}")
        return df
