"""
强化学习交易Agent

基于强化学习的自动交易策略。
依赖 stable-baselines3 和 gymnasium (可选，未安装时降级)。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd
from loguru import logger

from stock_model.strategy.base import ActionType, BaseStrategy, StrategyResult


class RLTradingAgent(BaseStrategy):
    """强化学习交易Agent

    使用PPO/DQN模型进行交易决策。
    依赖 stable-baselines3 和 gymnasium，未安装时降级为规则策略。

    使用示例:
        agent = RLTradingAgent()
        # 训练(需要gymnasium和stable-baselines3)
        agent.train(env, timesteps=10000)
        # 预测
        result = agent.analyze("000001", df)
    """

    name = "rl_agent"
    description = "强化学习交易Agent (PPO/DQN)"

    def __init__(self, model_type: str = "ppo"):
        self.model_type = model_type
        self._model = None
        self._trained = False

    def analyze(self, symbol: str, df: pd.DataFrame) -> StrategyResult:
        """分析并给出交易建议

        如果模型已训练，使用模型预测；否则使用简单规则。
        """
        if self._trained and self._model is not None:
            return self._predict(symbol, df)
        else:
            return self._rule_based(symbol, df)

    def evaluate(self, symbol: str, df: pd.DataFrame) -> dict:
        """评估策略表现"""
        from stock_model.strategy.engine import BacktestEngine

        engine = BacktestEngine(initial_cash=100000)
        result = engine.run(self, df, symbol)
        return result.metrics

    def train(self, env: Any, timesteps: int = 10000) -> None:
        """训练RL模型

        Args:
            env: gymnasium环境
            timesteps: 训练步数
        """
        try:
            from stable_baselines3 import PPO, DQN

            if self.model_type.lower() == "dqn":
                self._model = DQN("MlpPolicy", env, verbose=1)
            else:
                self._model = PPO("MlpPolicy", env, verbose=1)

            logger.info(f"开始训练 {self.model_type} 模型, 步数={timesteps}")
            self._model.learn(total_timesteps=timesteps)
            self._trained = True
            logger.info("训练完成")

        except ImportError:
            logger.warning(
                "stable-baselines3 或 gymnasium 未安装，无法训练RL模型。"
                "请安装: pip install stable-baselines3 gymnasium"
            )
            logger.info("将使用规则策略作为降级方案")

    def _predict(self, symbol: str, df: pd.DataFrame) -> StrategyResult:
        """使用RL模型预测"""
        try:
            # 提取特征
            obs = self._extract_features(df)
            action, _ = self._model.predict(obs, deterministic=True)

            # 映射动作
            action_map = {0: ActionType.HOLD, 1: ActionType.BUY, 2: ActionType.SELL}
            mapped_action = action_map.get(int(action), ActionType.HOLD)

            return StrategyResult(
                symbol=symbol,
                action=mapped_action,
                confidence=0.7,
                reason=f"RL模型({self.model_type})预测: action={int(action)}",
            )

        except Exception as e:
            logger.warning(f"RL模型预测失败: {e}, 降级为规则策略")
            return self._rule_based(symbol, df)

    def _rule_based(self, symbol: str, df: pd.DataFrame) -> StrategyResult:
        """规则策略(降级方案)"""
        if len(df) < 20:
            return StrategyResult(
                symbol=symbol, action=ActionType.HOLD, confidence=0.3, reason="数据不足"
            )

        ma5 = df["close"].rolling(5).mean().iloc[-1]
        ma20 = df["close"].rolling(20).mean().iloc[-1]
        current = df["close"].iloc[-1]

        if ma5 > ma20 and current > ma5:
            return StrategyResult(
                symbol=symbol, action=ActionType.BUY, confidence=0.6, reason="RL降级: 均线多头"
            )
        elif ma5 < ma20 and current < ma5:
            return StrategyResult(
                symbol=symbol, action=ActionType.SELL, confidence=0.6, reason="RL降级: 均线空头"
            )
        else:
            return StrategyResult(
                symbol=symbol, action=ActionType.HOLD, confidence=0.4, reason="RL降级: 均线纠缠"
            )

    @staticmethod
    def _extract_features(df: pd.DataFrame) -> np.ndarray:
        """提取特征向量"""
        if len(df) < 20:
            return np.zeros(10)

        features = []
        close = df["close"]

        # 收益率
        features.append(close.pct_change(1).iloc[-1])
        features.append(close.pct_change(5).iloc[-1])
        features.append(close.pct_change(20).iloc[-1])

        # 均线偏离
        ma5 = close.rolling(5).mean().iloc[-1]
        ma20 = close.rolling(20).mean().iloc[-1]
        features.append((close.iloc[-1] / ma5 - 1) if ma5 > 0 else 0)
        features.append((close.iloc[-1] / ma20 - 1) if ma20 > 0 else 0)

        # 波动率
        features.append(close.pct_change().rolling(20).std().iloc[-1])

        # 成交量变化
        if "volume" in df.columns:
            vol_ma5 = df["volume"].rolling(5).mean().iloc[-1]
            features.append(df["volume"].iloc[-1] / vol_ma5 - 1 if vol_ma5 > 0 else 0)
        else:
            features.append(0.0)

        # RSI近似
        delta = close.diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean().iloc[-1]
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean().iloc[-1]
        rs = gain / loss if loss > 0 else 100
        features.append(1 - 1 / (1 + rs))

        # 价格位置(20日高低)
        high20 = close.rolling(20).max().iloc[-1]
        low20 = close.rolling(20).min().iloc[-1]
        range_val = high20 - low20
        features.append((close.iloc[-1] - low20) / range_val if range_val > 0 else 0.5)

        # 均线斜率
        features.append((ma5 / ma20 - 1) if ma20 > 0 else 0)

        return np.array(features, dtype=np.float32)