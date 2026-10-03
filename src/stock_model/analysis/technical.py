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

from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger

try:
    import pandas_ta as ta
    HAS_PANDAS_TA = True
except ImportError:
    ta = None
    HAS_PANDAS_TA = False

from stock_model.config.settings import get_settings


class TechnicalAnalysis:
    """技术分析器"""

    def __init__(self):
        self.settings = get_settings().analysis
        if not HAS_PANDAS_TA:
            logger.warning(
                "pandas-ta 未安装，技术指标(MACD/RSI/BOLL/ATR/OBV)不可用。"
                "请安装: pip install stock-model[ta]"
            )

    # ==================== 趋势指标 ====================

    def ma(self, df: pd.DataFrame, periods: Optional[list[int]] = None) -> pd.DataFrame:
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

    def ema(self, df: pd.DataFrame, periods: Optional[list[int]] = None) -> pd.DataFrame:
        """计算指数移动均线"""
        df = df.copy()
        periods = periods or self.settings.ma_periods
        for period in periods:
            df[f"ema{period}"] = df["close"].ewm(span=period, adjust=False).mean()
        return df

    def macd(
        self,
        df: pd.DataFrame,
        fast: Optional[int] = None,
        slow: Optional[int] = None,
        signal: Optional[int] = None,
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

        macd_result = ta.macd(df["close"], fast=fast, slow=slow, signal=signal) if HAS_PANDAS_TA else None
        if macd_result is not None:
            df = pd.concat([df, macd_result], axis=1)

        return df

    # ==================== 动量指标 ====================

    def rsi(
        self,
        df: pd.DataFrame,
        period: Optional[int] = None,
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

        rsi_result = ta.rsi(df["close"], length=period) if HAS_PANDAS_TA else None
        if rsi_result is not None:
            df[f"rsi{period}"] = rsi_result

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
        period: Optional[int] = None,
        std_dev: Optional[float] = None,
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

        boll_result = ta.bbands(df["close"], length=period, std=std_dev) if HAS_PANDAS_TA else None
        if boll_result is not None:
            df = pd.concat([df, boll_result], axis=1)

        return df

    def atr(self, df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
        """计算ATR (真实波动范围)"""
        df = df.copy()
        atr_result = ta.atr(df["high"], df["low"], df["close"], length=period) if HAS_PANDAS_TA else None
        if atr_result is not None:
            df[f"atr{period}"] = atr_result
        return df

    # ==================== 量价指标 ====================

    def obv(self, df: pd.DataFrame) -> pd.DataFrame:
        """计算OBV (能量潮)"""
        df = df.copy()
        df["obv"] = ta.obv(df["close"], df["volume"]) if HAS_PANDAS_TA else np.nan
        return df

    def volume_ma(self, df: pd.DataFrame, periods: list[int] = [5, 10, 20]) -> pd.DataFrame:
        """计算成交量均线"""
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
        logger.info(f"执行综合技术分析, 数据量: {len(df)} 条")
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

        logger.info(f"技术分析完成, 指标列数: {len(df.columns)}")
        return df