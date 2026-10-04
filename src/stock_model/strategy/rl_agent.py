"""
强化学习交易Agent

基于强化学习的自动交易策略。
支持:
  - PPO/DQN模型训练 (依赖 stable-baselines3 + gymnasium)
  - 模型保存/加载
  - 训练监控回调
  - 规则策略降级 (无依赖时)
  - TradingEnv集成

依赖 stable-baselines3 和 gymnasium (可选，未安装时降级)。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from loguru import logger

from stock_model.strategy.base import ActionType, BaseStrategy, StrategyResult


class RLTradingAgent(BaseStrategy):
    """强化学习交易Agent

    使用PPO/DQN模型进行交易决策。
    依赖 stable-baselines3 和 gymnasium，未安装时降级为规则策略。

    使用示例:
        # 训练
        from stock_model.strategy.trading_env import TradingEnv
        env = TradingEnv(df, initial_balance=100000)
        agent = RLTradingAgent(model_type="ppo")
        metrics = agent.train(env, timesteps=10000)

        # 保存/加载
        agent.save("models/rl_agent")
        agent.load("models/rl_agent")

        # 预测
        result = agent.analyze("000001", df)

        # 评估
        metrics = agent.evaluate("000001", df)
    """

    name = "rl_agent"
    description = "强化学习交易Agent (PPO/DQN)"

    def __init__(self, model_type: str = "ppo"):
        """初始化

        Args:
            model_type: 模型类型 ("ppo" 或 "dqn")
        """
        self.model_type = model_type.lower()
        self._model = None
        self._trained = False
        self._training_metrics: list[dict] = []

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

    def train(
        self,
        env: Any,
        timesteps: int = 10000,
        save_path: str | None = None,
        eval_env: Any | None = None,
        eval_freq: int = 1000,
        verbose: int = 1,
        **kwargs: Any,
    ) -> dict:
        """训练RL模型

        Args:
            env: gymnasium环境(TradingEnv实例)
            timesteps: 训练步数
            save_path: 训练完成后保存路径(可选)
            eval_env: 评估环境(可选)
            eval_freq: 评估频率(步数)
            verbose: 日志级别(0=静默, 1=信息, 2=详细)
            **kwargs: 传递给模型构造函数的额外参数

        Returns:
            训练指标字典
        """
        try:
            from stable_baselines3 import DQN, PPO
            from stable_baselines3.common.callbacks import (
                BaseCallback,
                EvalCallback,
            )

            # 训练监控回调
            class TrainingLogCallback(BaseCallback):
                """训练日志回调"""

                def __init__(self, log_freq: int = 1000, verbose: int = 1):
                    super().__init__()
                    self.log_freq = log_freq
                    self.verbose = verbose
                    self.episode_rewards: list[float] = []
                    self.episode_lengths: list[int] = []

                def _on_step(self) -> bool:
                    if self.verbose >= 1 and self.num_timesteps % self.log_freq == 0:
                        logger.info(f"训练进度: {self.num_timesteps}/{timesteps}步")
                    return True

            # 创建模型
            model_cls = DQN if self.model_type == "dqn" else PPO
            self._model = model_cls(
                "MlpPolicy",
                env,
                verbose=verbose,
                **kwargs,
            )

            logger.info(f"开始训练 {self.model_type.upper()} 模型, 步数={timesteps}")

            # 训练回调
            callbacks = [TrainingLogCallback(verbose=verbose)]

            # 评估回调
            if eval_env is not None:
                eval_callback = EvalCallback(
                    eval_env,
                    best_model_save_path="./rl_best_model/",
                    log_path="./rl_logs/",
                    eval_freq=eval_freq,
                    deterministic=True,
                    render=False,
                    verbose=0,
                )
                callbacks.append(eval_callback)

            # 训练
            self._model.learn(
                total_timesteps=timesteps,
                callback=callbacks,
            )

            self._trained = True
            logger.info(f"训练完成: {self.model_type.upper()}, {timesteps}步")

            # 保存
            if save_path:
                self.save(save_path)

            # 收集训练指标
            metrics = {
                "model_type": self.model_type,
                "timesteps": timesteps,
                "trained": True,
            }

            # 尝试获取最终评估结果
            if hasattr(env, "total_value"):
                metrics["final_value"] = env.total_value

            self._training_metrics.append(metrics)
            return metrics

        except ImportError:
            logger.warning(
                "stable-baselines3 或 gymnasium 未安装，无法训练RL模型。"
                "请安装: pip install stable-baselines3 gymnasium"
            )
            logger.info("将使用规则策略作为降级方案")
            return {"model_type": self.model_type, "trained": False, "error": "依赖缺失"}

    def save(self, path: str) -> None:
        """保存模型

        Args:
            path: 保存路径(不含扩展名)
        """
        if self._model is None:
            logger.warning("没有可保存的模型")
            return

        save_dir = Path(path).parent
        save_dir.mkdir(parents=True, exist_ok=True)

        self._model.save(path)
        logger.info(f"模型已保存: {path}")

    def load(self, path: str) -> None:
        """加载模型

        Args:
            path: 模型路径(不含扩展名)
        """
        try:
            from stable_baselines3 import DQN, PPO

            model_cls = DQN if self.model_type == "dqn" else PPO
            self._model = model_cls.load(path)
            self._trained = True
            logger.info(f"模型已加载: {path}")
        except ImportError:
            logger.warning("stable-baselines3 未安装，无法加载模型")
        except FileNotFoundError:
            logger.warning(f"模型文件不存在: {path}")

    def train_with_data(
        self,
        df: pd.DataFrame,
        timesteps: int = 10000,
        save_path: str | None = None,
        **kwargs: Any,
    ) -> dict:
        """使用DataFrame数据训练(自动创建TradingEnv)

        Args:
            df: 行情数据
            timesteps: 训练步数
            save_path: 保存路径
            **kwargs: TradingEnv参数

        Returns:
            训练指标
        """
        try:
            from stock_model.strategy.trading_env import TradingEnv

            env = TradingEnv(df, **kwargs)
            return self.train(env, timesteps=timesteps, save_path=save_path)

        except ImportError:
            logger.warning("gymnasium 未安装，无法创建训练环境")
            return {"model_type": self.model_type, "trained": False, "error": "依赖缺失"}

    @property
    def is_trained(self) -> bool:
        """模型是否已训练"""
        return self._trained

    @property
    def training_history(self) -> list[dict]:
        """训练历史"""
        return self._training_metrics.copy()

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
                symbol=symbol,
                action=ActionType.BUY,
                confidence=0.6,
                reason="RL降级: 均线多头",
            )
        elif ma5 < ma20 and current < ma5:
            return StrategyResult(
                symbol=symbol,
                action=ActionType.SELL,
                confidence=0.6,
                reason="RL降级: 均线空头",
            )
        else:
            return StrategyResult(
                symbol=symbol,
                action=ActionType.HOLD,
                confidence=0.4,
                reason="RL降级: 均线纠缠",
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
