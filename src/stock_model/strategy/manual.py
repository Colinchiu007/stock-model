"""
手动策略模块

第一期手动分析策略，基于技术分析+基本面分析给出建议。
第二期将扩展为自动量化策略。
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger

from stock_model.analysis.fundamental import FundamentalAnalysis
from stock_model.analysis.signals import SignalGenerator, SignalType, SignalStrength
from stock_model.analysis.technical import TechnicalAnalysis
from stock_model.strategy.base import ActionType, BaseStrategy, StrategyResult


class ManualStrategy(BaseStrategy):
    """
    手动策略

    综合技术分析和基本面分析给出操作建议:
    1. 技术信号统计 (买入/卖出信号数量和强度)
    2. 基本面评分
    3. 趋势判断
    4. 综合决策
    """

    name = "manual"
    description = "手动综合策略 - 技术分析+基本面分析"

    def __init__(self):
        self.ta = TechnicalAnalysis()
        self.signals = SignalGenerator()
        self.fundamental = FundamentalAnalysis()

    def analyze(self, symbol: str, df: pd.DataFrame) -> StrategyResult:
        """
        综合分析

        Args:
            symbol: 股票代码
            df: 行情数据

        Returns:
            策略结果
        """
        # 1. 技术分析
        df_with_indicators = self.ta.analyze_all(df)

        # 2. 生成信号
        all_signals = self.signals.generate_all(df_with_indicators, symbol)

        # 3. 统计信号
        buy_signals = [s for s in all_signals if s.signal_type == SignalType.BUY]
        sell_signals = [s for s in all_signals if s.signal_type == SignalType.SELL]

        # 计算信号强度加权分数
        strength_weight = {
            SignalStrength.WEAK: 1,
            SignalStrength.MEDIUM: 2,
            SignalStrength.STRONG: 3,
        }
        buy_score = sum(strength_weight[s.strength] for s in buy_signals)
        sell_score = sum(strength_weight[s.strength] for s in sell_signals)

        # 4. 趋势判断
        trend = self._judge_trend(df_with_indicators)

        # 5. 综合决策
        net_score = buy_score - sell_score

        if net_score >= 3:
            action = ActionType.BUY
            confidence = min(0.95, 0.6 + net_score * 0.05)
            reason = f"技术面偏多 (买入信号={len(buy_signals)}, 卖出信号={len(sell_signals)}, 趋势={trend})"
        elif net_score <= -3:
            action = ActionType.SELL
            confidence = min(0.95, 0.6 + abs(net_score) * 0.05)
            reason = f"技术面偏空 (买入信号={len(buy_signals)}, 卖出信号={len(sell_signals)}, 趋势={trend})"
        else:
            action = ActionType.HOLD
            confidence = 0.5
            reason = f"技术面中性 (买入信号={len(buy_signals)}, 卖出信号={len(sell_signals)}, 趋势={trend})"

        # 6. 计算建议价格
        current_price = df["close"].iloc[-1]
        target_price = None
        stop_loss = None

        if action == ActionType.BUY:
            # 目标价: 基于ATR
            if "atr14" in df_with_indicators.columns:
                atr = df_with_indicators["atr14"].iloc[-1]
                target_price = current_price + 2 * atr
                stop_loss = current_price - 1.5 * atr

        elif action == ActionType.SELL:
            if "atr14" in df_with_indicators.columns:
                atr = df_with_indicators["atr14"].iloc[-1]
                stop_loss = current_price + 1.5 * atr

        # 7. 建议仓位
        position_pct = self._calculate_position(confidence, trend)

        # 8. 信号详情
        signal_details = [
            {"type": s.signal_type.value, "strength": s.strength.value, "reason": s.reason}
            for s in all_signals
        ]

        return StrategyResult(
            symbol=symbol,
            action=action,
            confidence=confidence,
            reason=reason,
            target_price=target_price,
            stop_loss=stop_loss,
            position_pct=position_pct,
            metadata={
                "trend": trend,
                "buy_score": buy_score,
                "sell_score": sell_score,
                "signals": signal_details,
                "current_price": current_price,
            },
        )

    def evaluate(self, symbol: str, df: pd.DataFrame) -> dict:
        """
        简单回测评估

        基于历史数据模拟策略表现
        """
        logger.info(f"评估策略 [{self.name}]: {symbol}")

        results = {
            "total_trades": 0,
            "win_trades": 0,
            "lose_trades": 0,
            "total_return": 0.0,
            "max_drawdown": 0.0,
        }

        # 简单回测: 滚动窗口
        window = 60  # 60日窗口
        if len(df) < window + 10:
            return results

        trades = []
        for i in range(window, len(df) - 10, 10):
            window_df = df.iloc[i - window : i]
            result = self.analyze(symbol, window_df)

            if result.action == ActionType.BUY:
                entry_price = df["close"].iloc[i]
                exit_price = df["close"].iloc[min(i + 10, len(df) - 1)]
                trade_return = (exit_price - entry_price) / entry_price
                trades.append(trade_return)

        if trades:
            results["total_trades"] = len(trades)
            results["win_trades"] = sum(1 for t in trades if t > 0)
            results["lose_trades"] = sum(1 for t in trades if t <= 0)
            results["total_return"] = sum(trades)
            results["win_rate"] = results["win_trades"] / results["total_trades"]

        return results

    def _judge_trend(self, df: pd.DataFrame) -> str:
        """
        判断趋势

        Returns:
            "up" / "down" / "sideways"
        """
        if len(df) < 60:
            return "unknown"

        close = df["close"]

        # MA趋势
        ma5 = close.rolling(5).mean().iloc[-1]
        ma20 = close.rolling(20).mean().iloc[-1]
        ma60 = close.rolling(60).mean().iloc[-1] if len(df) >= 60 else ma20

        current = close.iloc[-1]

        if current > ma5 > ma20 > ma60:
            return "up"
        elif current < ma5 < ma20 < ma60:
            return "down"
        else:
            return "sideways"

    def _calculate_position(self, confidence: float, trend: str) -> float:
        """
        计算建议仓位

        Args:
            confidence: 信心度 0-1
            trend: 趋势方向

        Returns:
            建议仓位比例 0-100
        """
        base_position = confidence * 60  # 基础仓位

        # 趋势加成
        trend_bonus = {"up": 20, "sideways": 0, "down": -20, "unknown": 0}
        base_position += trend_bonus.get(trend, 0)

        return max(0, min(100, base_position))