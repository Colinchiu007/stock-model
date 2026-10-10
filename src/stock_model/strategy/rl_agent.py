"""
强化学习交易Agent

基于强化学习的自动交易策略。
支持:
  - PPO/DQN模型训练 (依赖 stable-baselines3 + gymnasium)
  - 模型保存/加载
  - 训练监控回调
  - 规则策略降级 (无依赖时)
  - TradingEnv集成
  - Episode级评估 (多轮评估+统计指标)
  - 训练-评估-选择闭环 (train_evaluate_select)

依赖 stable-baselines3 和 gymnasium (可选，未安装时降级)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
from loguru import logger

from stock_model.strategy.base import ActionType, BaseStrategy, StrategyResult

if TYPE_CHECKING:
    import pandas as pd


@dataclass
class EvaluationResult:
    """RL Agent评估结果

    Attributes:
        model_type: 模型类型 (ppo/dqn/rule_based)
        n_episodes: 评估轮数
        total_rewards: 每轮总奖励列表
        episode_lengths: 每轮步数列表
        final_values: 每轮最终组合价值列表
        mean_reward: 平均奖励
        std_reward: 奖励标准差
        mean_value: 平均最终价值
        win_rate: 盈利轮比例(最终价值>初始资金)
        sharpe_ratio: 平均奖励的年化夏普比率
        max_drawdown: 最大回撤
        backtest_metrics: 回测指标(如果有)
    """

    model_type: str
    n_episodes: int = 0
    total_rewards: list[float] = field(default_factory=list)
    episode_lengths: list[int] = field(default_factory=list)
    final_values: list[float] = field(default_factory=list)
    mean_reward: float = 0.0
    std_reward: float = 0.0
    mean_value: float = 0.0
    win_rate: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0
    backtest_metrics: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return (
            f"EvaluationResult({self.model_type}, "
            f"episodes={self.n_episodes}, "
            f"mean_reward={self.mean_reward:.4f}, "
            f"mean_value={self.mean_value:.0f}, "
            f"win_rate={self.win_rate:.1%}, "
            f"sharpe={self.sharpe_ratio:.2f}, "
            f"max_dd={self.max_drawdown:.2%})"
        )

    def summary(self) -> dict[str, Any]:
        """返回评估摘要字典"""
        return {
            "model_type": self.model_type,
            "n_episodes": self.n_episodes,
            "mean_reward": self.mean_reward,
            "std_reward": self.std_reward,
            "mean_value": self.mean_value,
            "win_rate": self.win_rate,
            "sharpe_ratio": self.sharpe_ratio,
            "max_drawdown": self.max_drawdown,
        }


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

        # 保存/加载（load 返回是否成功，务必检查）
        agent.save("models/rl_agent")
        if not agent.load("models/rl_agent"):
            logger.warning("模型加载失败，将使用规则策略降级")

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
        # stable_baselines3 是可选依赖且无类型信息, 故用 Any:
        # 不写注解时 mypy 会把 _model 推断成 None, 于是 self._model.learn /
        # self._model.predict 被报成「"None" has no attribute ...」。
        # 属注解缺失, 不是运行时缺陷 —— analyze() 有 `_model is not None` 守卫。
        self._model: Any = None
        self._trained = False
        self._training_metrics: list[dict] = []

    def analyze(self, symbol: str, df: pd.DataFrame) -> StrategyResult:
        """分析并给出交易建议

        如果模型已训练，使用模型预测；否则使用简单规则。
        """
        if self._trained and self._model is not None:
            return self._predict(symbol, df)
        return self._rule_based(symbol, df)

    def evaluate(self, symbol: str, df: pd.DataFrame) -> dict:
        """评估策略表现(回测模式)"""
        from stock_model.strategy.engine import BacktestEngine

        engine = BacktestEngine(initial_cash=100000)
        result = engine.run(self, df, symbol)
        return result.metrics

    def evaluate_episodes(
        self,
        env: Any,
        n_episodes: int = 10,
        deterministic: bool = True,
    ) -> EvaluationResult:
        """Episode级评估: 运行多轮评估并收集统计指标

        Args:
            env: gymnasium环境(TradingEnv/MultiStockTradingEnv实例)
            n_episodes: 评估轮数
            deterministic: 是否使用确定性策略

        Returns:
            EvaluationResult评估结果
        """
        total_rewards: list[float] = []
        episode_lengths: list[int] = []
        final_values: list[float] = []

        for _ep in range(n_episodes):
            obs, _info = env.reset()
            episode_reward = 0.0
            step_count = 0
            done = False

            while not done:
                if self._trained and self._model is not None:
                    try:
                        action, _ = self._model.predict(obs, deterministic=deterministic)
                        if isinstance(action, np.ndarray):
                            action = int(action.flat[0])
                        else:
                            action = int(action)
                    except (ValueError, RuntimeError, IndexError):
                        action = 0  # HOLD as fallback
                else:
                    action = 0  # 未训练时HOLD

                obs, reward, terminated, truncated, _info = env.step(action)
                episode_reward += reward
                step_count += 1
                done = terminated or truncated

            total_rewards.append(episode_reward)
            episode_lengths.append(step_count)
            final_value = getattr(env, "total_value", 0.0)
            if final_value == 0.0 and hasattr(env, "_total_value"):
                final_value = env._total_value
            final_values.append(final_value)

        # 计算统计指标
        rewards_arr = np.array(total_rewards)
        values_arr = np.array(final_values)
        initial_balance = getattr(env, "initial_balance", 100000.0)

        mean_reward = float(np.mean(rewards_arr))
        std_reward = float(np.std(rewards_arr))
        mean_value = float(np.mean(values_arr))
        win_rate = float(np.mean(values_arr > initial_balance))

        # 夏普比率(年化)
        sharpe_ratio = float(mean_reward / std_reward * np.sqrt(252)) if std_reward > 0 else 0.0

        # 最大回撤(基于最终价值序列)
        if len(values_arr) > 1:
            peak = values_arr[0]
            max_dd = 0.0
            for v in values_arr:
                peak = max(peak, v)
                dd = (peak - v) / peak if peak > 0 else 0.0
                max_dd = max(max_dd, dd)
            max_drawdown = float(max_dd)
        else:
            max_drawdown = 0.0

        result = EvaluationResult(
            model_type=self.model_type if self._trained else "rule_based",
            n_episodes=n_episodes,
            total_rewards=total_rewards,
            episode_lengths=episode_lengths,
            final_values=final_values,
            mean_reward=mean_reward,
            std_reward=std_reward,
            mean_value=mean_value,
            win_rate=win_rate,
            sharpe_ratio=sharpe_ratio,
            max_drawdown=max_drawdown,
        )

        logger.info(f"评估完成: {result}")
        return result

    def compare_with_baseline(
        self,
        env: Any,
        df: pd.DataFrame,
        symbol: str,
        n_episodes: int = 10,
    ) -> dict[str, EvaluationResult]:
        """对比RL模型与规则基线策略

        Args:
            env: 评估环境
            df: 回测数据
            symbol: 股票代码
            n_episodes: 评估轮数

        Returns:
            {"rl": EvaluationResult, "baseline": EvaluationResult}
        """
        results: dict[str, EvaluationResult] = {}

        # RL模型评估
        if self._trained and self._model is not None:
            results["rl"] = self.evaluate_episodes(env, n_episodes=n_episodes)
        else:
            results["rl"] = EvaluationResult(model_type="untrained", n_episodes=0)

        # 前测基线
        from stock_model.strategy.engine import BacktestEngine

        engine = BacktestEngine(initial_cash=100000)
        bt_result = engine.run(self, df, symbol)

        baseline = EvaluationResult(
            model_type="rule_based",
            n_episodes=1,
            mean_value=bt_result.metrics.get("total_return", 0.0) * 100000 + 100000,
            backtest_metrics=bt_result.metrics,
        )
        results["baseline"] = baseline

        logger.info(
            f"对比完成: RL mean_reward={results['rl'].mean_reward:.4f}, "
            f"Baseline return={bt_result.metrics.get('total_return', 0.0):.2%}"
        )
        return results

    def train_evaluate_select(
        self,
        train_env: Any,
        eval_env: Any,
        df: pd.DataFrame,
        symbol: str,
        model_types: list[str] | None = None,
        timesteps: int = 10000,
        n_eval_episodes: int = 10,
    ) -> dict[str, Any]:
        """训练-评估-选择闭环

        对多个模型类型进行训练和评估，选择最佳模型。

        Args:
            train_env: 讛练环境
            eval_env: 评估环境
            df: 回测数据
            symbol: 股票代码
            model_types: 模型类型列表 (默认["ppo", "dqn"])
            timesteps: 训练步数
            n_eval_episodes: 评估轮数

        Returns:
            包含所有评估结果和最佳模型的字典
        """
        if model_types is None:
            model_types = ["ppo", "dqn"]

        eval_results: dict[str, EvaluationResult] = {}
        train_results: dict[str, dict] = {}
        best_model_type: str | None = None
        best_reward = float("-inf")

        for mt in model_types:
            logger.info(f"训练 {mt.upper()} 挆型...")
            self.model_type = mt.lower()
            self._model = None
            self._trained = False

            # 训练
            train_result = self.train(train_env, timesteps=timesteps)
            train_results[mt] = train_result

            if not train_result.get("trained", False):
                logger.warning(f"{mt.upper()} 训练失败，跳过评估")
                continue

            # 评估
            eval_result = self.evaluate_episodes(eval_env, n_episodes=n_eval_episodes)
            eval_results[mt] = eval_result

            # 选择最佳
            if eval_result.mean_reward > best_reward:
                best_reward = eval_result.mean_reward
                best_model_type = mt

        # 前测基线对比
        baseline_results = self.compare_with_baseline(
            eval_env, df, symbol, n_episodes=n_eval_episodes
        )

        result = {
            "train_results": train_results,
            "eval_results": {k: v.summary() for k, v in eval_results.items()},
            "baseline": baseline_results.get(
                "baseline", EvaluationResult(model_type="rule_based")
            ).summary(),
            "best_model_type": best_model_type,
            "best_reward": best_reward if best_model_type else None,
        }

        if best_model_type:
            logger.info(f"最佳模型: {best_model_type.upper()}, mean_reward={best_reward:.4f}")

        return result

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

    def load(self, path: str) -> bool:
        """加载模型

        Args:
            path: 模型路径(不含扩展名)

        Returns:
            bool: 是否加载成功

            注意: 此前失败时只 log 一条 warning 并返回 None, 调用方无法区分
            成功与失败 —— 只能去读日志。改为返回明确的布尔值, 并说明失败原因。
            ``is_trained`` 属性此前全项目无人使用, 形同虚设。
        """
        try:
            from stable_baselines3 import DQN, PPO

            model_cls = DQN if self.model_type == "dqn" else PPO
            self._model = model_cls.load(path)
            self._trained = True
            logger.info(f"模型已加载: {path}")
            return True
        except ImportError:
            logger.warning("stable-baselines3 未安装，无法加载模型")
            return False
        except FileNotFoundError:
            logger.warning(f"模型文件不存在: {path}")
            return False

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

        except (ValueError, RuntimeError, KeyError) as e:
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
        if ma5 < ma20 and current < ma5:
            return StrategyResult(
                symbol=symbol,
                action=ActionType.SELL,
                confidence=0.6,
                reason="RL降级: 均线空头",
            )
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
