"""
Gymnasium 交易环境

为强化学习Agent提供标准 gymnasium.Env 接口的交易环境。
支持:
  - 观察空间: 技术指标向量 (close/returns/MA/RSI/MACD等)
  - 动作空间: Discrete(3) - 0:HOLD, 1:BUY, 2:SELL
  - 奖励函数: 基于持仓收益 + 风险惩罚
  - 自定义交易成本(佣金+滑点)

依赖 gymnasium (可选，未安装时降级)。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from loguru import logger


class TradingEnv:
    """Gymnasium 交易环境 (兼容 gymnasium.Env 接口)

    基于历史行情数据的交易模拟环境，用于训练强化学习Agent。

    使用示例:
        env = TradingEnv(df, initial_balance=100000)
        obs, info = env.reset()
        for _ in range(1000):
            action = agent.predict(obs)
            obs, reward, terminated, truncated, info = env.step(action)
            if terminated or truncated:
                break

    注意:
        需要安装 gymnasium 才能使用完整的 Env 接口。
        未安装时，仍可作为简单模拟器使用。
    """

    metadata: dict[str, Any] = {"render_modes": ["human"]}

    def __init__(
        self,
        df: pd.DataFrame,
        initial_balance: float = 100000.0,
        commission_rate: float = 0.0003,
        slippage: float = 0.001,
        window_size: int = 20,
        reward_scaling: float = 1.0,
        max_position_pct: float = 0.95,
        min_shares: int = 100,
    ):
        """初始化交易环境

        Args:
            df: 行情数据(需含open/high/low/close/volume列)
            initial_balance: 初始资金
            commission_rate: 佣金率
            slippage: 滑点
            window_size: 观察窗口大小
            reward_scaling: 奖励缩放因子
            max_position_pct: 最大持仓比例
            min_shares: 最小交易股数
        """
        self.df = df.reset_index(drop=True)
        self.initial_balance = initial_balance
        self.commission_rate = commission_rate
        self.slippage = slippage
        self.window_size = window_size
        self.reward_scaling = reward_scaling
        self.max_position_pct = max_position_pct
        self.min_shares = min_shares

        # 动作空间: 0=HOLD, 1=BUY, 2=SELL
        self.action_space = None  # 延迟初始化(gymnasium依赖)
        self.observation_space = None

        # 内部状态
        self._current_step = 0
        self._balance = initial_balance
        self._shares = 0
        self._cost_price = 0.0
        self._total_value = initial_balance
        self._prev_value = initial_balance
        self._done = False
        self._info: dict = {}

        # 预计算特征
        self._features = self._compute_features()

        # 尝试初始化gymnasium空间
        self._init_gym_spaces()

    def _init_gym_spaces(self) -> None:
        """尝试初始化gymnasium空间"""
        try:
            import gymnasium as gym

            n_features = self._features.shape[1] if len(self._features.shape) > 1 else 10
            self.observation_space = gym.spaces.Box(
                low=-np.inf,
                high=np.inf,
                shape=(n_features,),
                dtype=np.float32,
            )
            self.action_space = gym.spaces.Discrete(3)
        except ImportError:
            logger.debug("gymnasium 未安装, 使用简化接口")

    def _compute_features(self) -> np.ndarray:
        """预计算观察特征"""
        df = self.df
        if "close" not in df.columns:
            return np.zeros((len(df), 10), dtype=np.float32)

        close = df["close"]
        features_list = []

        # 归一化收盘价
        if close.iloc[0] > 0:
            features_list.append((close / close.iloc[0]).values)

        # 收益率
        returns = close.pct_change().fillna(0).values
        features_list.append(returns)

        # MA偏离
        for period in [5, 10, 20]:
            if len(close) >= period:
                ma = close.rolling(period).mean()
                ratio = (close / ma - 1).fillna(0).values
                features_list.append(ratio)

        # 波动率
        if len(close) > 1:
            vol = close.pct_change().rolling(20).std().fillna(0).values
            features_list.append(vol)

        # RSI近似
        if len(close) > 14:
            delta = close.diff()
            gain = delta.where(delta > 0, 0).rolling(14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rs = gain / loss.replace(0, 1)
            rsi = (1 - 1 / (1 + rs)).fillna(0.5).values
            features_list.append(rsi)

        # 成交量变化
        if "volume" in df.columns:
            vol_ma5 = df["volume"].rolling(5).mean()
            vol_ratio = (df["volume"] / vol_ma5 - 1).fillna(0).values
            features_list.append(vol_ratio)

        # 对齐长度
        max_len = len(close)
        aligned = []
        for f in features_list:
            arr = np.array(f, dtype=np.float32)
            if len(arr) < max_len:
                padded = np.zeros(max_len, dtype=np.float32)
                padded[-len(arr) :] = arr
                aligned.append(padded)
            else:
                aligned.append(arr[:max_len])

        return np.column_stack(aligned) if aligned else np.zeros((max_len, 10), dtype=np.float32)

    def reset(self, seed: int | None = None, **kwargs) -> tuple[np.ndarray, dict]:
        """重置环境

        Returns:
            (observation, info)
        """
        if seed is not None:
            np.random.seed(seed)

        self._current_step = self.window_size
        self._balance = self.initial_balance
        self._shares = 0
        self._cost_price = 0.0
        self._total_value = self.initial_balance
        self._prev_value = self.initial_balance
        self._done = False
        self._info = {
            "balance": self._balance,
            "shares": self._shares,
            "total_value": self._total_value,
            "step": 0,
        }

        obs = self._get_observation()
        return obs, self._info

    def step(self, action: int) -> tuple[np.ndarray, float, bool, bool, dict]:
        """执行一步

        Args:
            action: 0=HOLD, 1=BUY, 2=SELL

        Returns:
            (observation, reward, terminated, truncated, info)
        """
        if self._done:
            return self._get_observation(), 0.0, True, False, self._info

        self._prev_value = self._total_value
        current_price = float(self.df["close"].iloc[self._current_step])

        # 执行交易
        if action == 1:  # BUY
            self._buy(current_price)
        elif action == 2:  # SELL
            self._sell(current_price)

        # 前进一步
        self._current_step += 1

        # 计算总价值
        self._total_value = self._balance + self._shares * current_price

        # 计算奖励
        reward = self._compute_reward()

        # 检查终止条件
        terminated = self._current_step >= len(self.df) - 1
        truncated = self._total_value <= 0

        # 更新info
        self._info = {
            "balance": self._balance,
            "shares": self._shares,
            "cost_price": self._cost_price,
            "current_price": current_price,
            "total_value": self._total_value,
            "step": self._current_step,
            "action": action,
            "pnl": self._total_value - self.initial_balance,
            "pnl_pct": (self._total_value / self.initial_balance - 1),
        }

        self._done = terminated or truncated

        obs = self._get_observation()
        return obs, reward, terminated, truncated, self._info

    def _buy(self, price: float) -> None:
        """买入"""
        if self._balance <= 0:
            return

        # 计算可买股数
        max_invest = self._balance * self.max_position_pct
        actual_price = price * (1 + self.slippage)
        commission = max_invest * self.commission_rate
        investable = max_invest - commission
        shares = int(investable / actual_price / self.min_shares) * self.min_shares

        if shares <= 0:
            return

        cost = shares * actual_price + shares * actual_price * self.commission_rate
        if cost > self._balance:
            return

        # 更新持仓(均价)
        if self._shares > 0:
            total_cost = self._cost_price * self._shares + cost
            self._shares += shares
            self._cost_price = total_cost / self._shares
        else:
            self._shares = shares
            self._cost_price = actual_price

        self._balance -= cost

    def _sell(self, price: float) -> None:
        """卖出"""
        if self._shares <= 0:
            return

        actual_price = price * (1 - self.slippage)
        revenue = self._shares * actual_price
        commission = revenue * self.commission_rate
        self._balance += revenue - commission
        self._shares = 0
        self._cost_price = 0.0

    def _compute_reward(self) -> float:
        """计算奖励"""
        # 基于总价值变化
        value_change = self._total_value - self._prev_value
        reward = value_change / self.initial_balance * self.reward_scaling

        # 持仓风险惩罚
        if self._shares > 0:
            current_price = float(self.df["close"].iloc[min(self._current_step, len(self.df) - 1)])
            unrealized_pnl = (current_price / self._cost_price - 1) if self._cost_price > 0 else 0
            # 大幅亏损惩罚
            if unrealized_pnl < -0.05:
                reward -= abs(unrealized_pnl) * 0.5

        return float(reward)

    def _get_observation(self) -> np.ndarray:
        """获取当前观察"""
        n_features = self._features.shape[1] if len(self._features.shape) > 1 else 10
        if self._current_step < self.window_size or self._current_step >= len(self._features):
            return np.zeros(n_features, dtype=np.float32)

        # 取最近window_size步的特征
        start = max(0, self._current_step - self.window_size)
        window = self._features[start : self._current_step + 1]

        # 展平为1D向量
        obs = window.flatten().astype(np.float32)

        # 固定长度(截断或填充)
        expected_len = self.window_size * n_features
        if len(obs) < expected_len:
            padded = np.zeros(expected_len, dtype=np.float32)
            padded[-len(obs) :] = obs
            obs = padded
        elif len(obs) > expected_len:
            obs = obs[-expected_len:]

        # 处理NaN/Inf
        obs = np.nan_to_num(obs, nan=0.0, posinf=0.0, neginf=0.0)

        return obs

    @property
    def current_step(self) -> int:
        """当前步数"""
        return self._current_step

    @property
    def total_value(self) -> float:
        """当前总价值"""
        return self._total_value

    def render(self, mode: str = "human") -> None:
        """渲染(简化版)"""
        if self._current_step < len(self.df):
            price = float(self.df["close"].iloc[self._current_step])
            pnl = self._total_value - self.initial_balance
            pnl_pct = (self._total_value / self.initial_balance - 1) * 100
            logger.info(
                f"Step {self._current_step}: "
                f"价格={price:.2f} | 持仓={self._shares}股 | "
                f"余额={self._balance:.0f} | 总值={self._total_value:.0f} | "
                f"盈亏={pnl:+.0f}({pnl_pct:+.1f}%)"
            )
