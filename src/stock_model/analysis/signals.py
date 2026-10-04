"""
交易信号生成模块

功能:
  - 技术信号 (金叉/死叉/超买/超卖)
  - 综合信号
  - 信号过滤
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import pandas as pd
from loguru import logger

from stock_model.analysis.technical import TechnicalAnalysis


class SignalType(str, Enum):
    """信号类型"""

    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


class SignalStrength(str, Enum):
    """信号强度"""

    STRONG = "strong"
    MEDIUM = "medium"
    WEAK = "weak"


@dataclass
class Signal:
    """交易信号"""

    symbol: str
    signal_type: SignalType
    strength: SignalStrength
    reason: str
    price: float = 0.0
    indicators: dict = field(default_factory=dict)
    timestamp: pd.Timestamp | None = None

    def __str__(self) -> str:
        return (
            f"Signal({self.symbol}: {self.signal_type.value} | "
            f"{self.strength.value} | {self.reason})"
        )


class SignalGenerator:
    """信号生成器"""

    def __init__(self):
        self.ta = TechnicalAnalysis()

    @staticmethod
    def _get_scalar(row: pd.Series, col: str) -> float:
        """从Series行中获取标量值，处理重复列名情况"""
        val = row[col]
        if isinstance(val, pd.Series):
            val = val.iloc[0]
        if pd.isna(val):
            return 0.0
        return float(val)

    @staticmethod
    def _find_column(df: pd.DataFrame, prefix: str) -> str | None:
        """查找以指定前缀开头的列名（不区分大小写）"""
        prefix_upper = prefix.upper()
        for col in df.columns:
            if col.upper().startswith(prefix_upper):
                return col
        return None

    def ma_cross_signal(self, df: pd.DataFrame, symbol: str = "") -> list[Signal]:
        """
        均线交叉信号

        短期均线上穿长期均线 -> 金叉(买入)
        短期均线下穿长期均线 -> 死叉(卖出)
        """
        # 仅在列不存在时才计算
        if "ma5" not in df.columns or "ma20" not in df.columns:
            df = self.ta.ma(df, periods=[5, 10, 20, 60])
        signals = []

        if len(df) < 2:
            return signals

        current = df.iloc[-1]
        prev = df.iloc[-2]

        # MA5 与 MA20 金叉/死叉
        if "ma5" in df.columns and "ma20" in df.columns:
            prev_ma5 = self._get_scalar(prev, "ma5")
            prev_ma20 = self._get_scalar(prev, "ma20")
            curr_ma5 = self._get_scalar(current, "ma5")
            curr_ma20 = self._get_scalar(current, "ma20")
            if prev_ma5 <= prev_ma20 and curr_ma5 > curr_ma20:
                signals.append(
                    Signal(
                        symbol=symbol,
                        signal_type=SignalType.BUY,
                        strength=SignalStrength.MEDIUM,
                        reason="MA5上穿MA20 (金叉)",
                        price=self._get_scalar(current, "close"),
                        timestamp=df.index[-1] if isinstance(df.index[-1], pd.Timestamp) else None,
                    )
                )
            elif prev_ma5 >= prev_ma20 and curr_ma5 < curr_ma20:
                signals.append(
                    Signal(
                        symbol=symbol,
                        signal_type=SignalType.SELL,
                        strength=SignalStrength.MEDIUM,
                        reason="MA5下穿MA20 (死叉)",
                        price=self._get_scalar(current, "close"),
                        timestamp=df.index[-1] if isinstance(df.index[-1], pd.Timestamp) else None,
                    )
                )

        return signals

    def macd_signal(self, df: pd.DataFrame, symbol: str = "") -> list[Signal]:
        """
        MACD信号

        MACD金叉 -> 买入
        MACD死叉 -> 卖出
        """
        # 仅在列不存在时才计算
        macd_col = self._find_column(df, "MACD_")
        signal_col = self._find_column(df, "MACDs_")
        if macd_col is None or signal_col is None:
            df = self.ta.macd(df)
            macd_col = self._find_column(df, "MACD_")
            signal_col = self._find_column(df, "MACDs_")

        signals = []

        if len(df) < 2 or macd_col is None or signal_col is None:
            return signals

        current = df.iloc[-1]
        prev = df.iloc[-2]

        prev_macd = self._get_scalar(prev, macd_col)
        prev_signal = self._get_scalar(prev, signal_col)
        curr_macd = self._get_scalar(current, macd_col)
        curr_signal = self._get_scalar(current, signal_col)

        # MACD金叉
        if prev_macd <= prev_signal and curr_macd > curr_signal:
            signals.append(
                Signal(
                    symbol=symbol,
                    signal_type=SignalType.BUY,
                    strength=SignalStrength.MEDIUM,
                    reason="MACD金叉",
                    price=self._get_scalar(current, "close"),
                )
            )
        # MACD死叉
        elif prev_macd >= prev_signal and curr_macd < curr_signal:
            signals.append(
                Signal(
                    symbol=symbol,
                    signal_type=SignalType.SELL,
                    strength=SignalStrength.MEDIUM,
                    reason="MACD死叉",
                    price=self._get_scalar(current, "close"),
                )
            )

        return signals

    def rsi_signal(self, df: pd.DataFrame, symbol: str = "") -> list[Signal]:
        """
        RSI信号

        RSI < 30 -> 超卖(买入)
        RSI > 70 -> 超买(卖出)
        """
        # 仅在列不存在时才计算
        rsi_col = self._find_column(df, "RSI")
        if rsi_col is None:
            df = self.ta.rsi(df)
            rsi_col = self._find_column(df, "RSI")

        signals = []

        if rsi_col is None or len(df) < 1:
            return signals

        current = df.iloc[-1]
        rsi_value = self._get_scalar(current, rsi_col)

        if rsi_value < 30:
            signals.append(
                Signal(
                    symbol=symbol,
                    signal_type=SignalType.BUY,
                    strength=SignalStrength.STRONG if rsi_value < 20 else SignalStrength.MEDIUM,
                    reason=f"RSI超卖 ({rsi_value:.1f})",
                    price=self._get_scalar(current, "close"),
                    indicators={"rsi": rsi_value},
                )
            )
        elif rsi_value > 70:
            signals.append(
                Signal(
                    symbol=symbol,
                    signal_type=SignalType.SELL,
                    strength=SignalStrength.STRONG if rsi_value > 80 else SignalStrength.MEDIUM,
                    reason=f"RSI超买 ({rsi_value:.1f})",
                    price=self._get_scalar(current, "close"),
                    indicators={"rsi": rsi_value},
                )
            )

        return signals

    def boll_signal(self, df: pd.DataFrame, symbol: str = "") -> list[Signal]:
        """
        布林带信号

        价格触及下轨 -> 买入
        价格触及上轨 -> 卖出
        """
        # 仅在列不存在时才计算
        lower_col = self._find_column(df, "BBL_")
        upper_col = self._find_column(df, "BBU_")
        if lower_col is None or upper_col is None:
            df = self.ta.boll(df)
            lower_col = self._find_column(df, "BBL_")
            upper_col = self._find_column(df, "BBU_")

        signals = []

        if lower_col is None or upper_col is None or len(df) < 1:
            return signals

        current = df.iloc[-1]
        close = self._get_scalar(current, "close")
        lower = self._get_scalar(current, lower_col)
        upper = self._get_scalar(current, upper_col)

        if close <= lower:
            signals.append(
                Signal(
                    symbol=symbol,
                    signal_type=SignalType.BUY,
                    strength=SignalStrength.STRONG,
                    reason=f"价格触及布林带下轨 (close={close:.2f}, lower={lower:.2f})",
                    price=close,
                )
            )
        elif close >= upper:
            signals.append(
                Signal(
                    symbol=symbol,
                    signal_type=SignalType.SELL,
                    strength=SignalStrength.STRONG,
                    reason=f"价格触及布林带上轨 (close={close:.2f}, upper={upper:.2f})",
                    price=close,
                )
            )

        return signals

    def generate_all(self, df: pd.DataFrame, symbol: str = "") -> list[Signal]:
        """
        生成所有信号

        Args:
            df: 行情数据
            symbol: 股票代码

        Returns:
            综合信号列表
        """
        logger.info(f"生成交易信号: {symbol}")
        all_signals = []

        all_signals.extend(self.ma_cross_signal(df, symbol))
        all_signals.extend(self.macd_signal(df, symbol))
        all_signals.extend(self.rsi_signal(df, symbol))
        all_signals.extend(self.boll_signal(df, symbol))

        # 统计信号
        buy_count = sum(1 for s in all_signals if s.signal_type == SignalType.BUY)
        sell_count = sum(1 for s in all_signals if s.signal_type == SignalType.SELL)
        logger.info(f"信号统计: 买入={buy_count}, 卖出={sell_count}")

        return all_signals

    def filter_signals(
        self,
        signals: list[Signal],
        min_strength: SignalStrength = SignalStrength.WEAK,
    ) -> list[Signal]:
        """
        过滤信号

        Args:
            signals: 信号列表
            min_strength: 最低信号强度

        Returns:
            过滤后的信号列表
        """
        strength_order = {
            SignalStrength.WEAK: 0,
            SignalStrength.MEDIUM: 1,
            SignalStrength.STRONG: 2,
        }
        min_level = strength_order[min_strength]
        return [s for s in signals if strength_order[s.strength] >= min_level]
