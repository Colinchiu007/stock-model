"""趋势过滤器

为什么需要
----------
分组对照实验（``experiments/run_dynamic_pool_test.py``）证明：
换成真正分散的股票池后，**上涨市仍然跑输基准 41%**，
且此时策略并非空仓踏空，而是在**亏损** —— 频繁反向操作。

实测根因（2019 年动态池）：

    SELL 信号共 6 次，其中 4 次（67%）发生在价格上涨趋势中

典型案例 002541（2019 年 +55%）：

    2019-04-03  收盘 10.22  > MA20 9.59   -> SELL   # 卖在上涨途中
    2019-05-08  收盘  9.58  < MA20 10.54  -> BUY    # 逆势抄底
    2019-10-11  收盘 12.92  > MA20 11.74  -> SELL   # 卖在阶段高点

即策略在**震荡市**里反复猜顶猜底有效，在**单边上涨**里被反复打脸。

设计
----
过滤器**只拦截逆势卖出**，不拦截其他动作：

- **强上涨趋势中禁止卖出** —— 主升段不猜顶，趋势跟随
- **强下跌趋势中禁止买入** —— 不接飞刀
- 横盘/不确定趋势: **放行原信号**，不改变震荡市表现

这是最小干预：只去掉"在明显趋势中逆向操作"这一类动作，
其余行为完全交给原策略。若指标判断错了，过滤器形同虚设，
不会引入新的伤害。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import pandas as pd


class TrendState(str, Enum):
    """趋势状态"""

    STRONG_UP = "strong_up"  # 强上涨
    MILD_UP = "mild_up"  # 温和上涨
    SIDEWAYS = "sideways"  # 横盘
    MILD_DOWN = "mild_down"  # 温和下跌
    STRONG_DOWN = "strong_down"  # 强下跌
    UNKNOWN = "unknown"  # 数据不足


@dataclass
class TrendFilterConfig:
    """趋势过滤配置

    Attributes:
        ma_period: 趋势均线周期
        strong_up_threshold: 收盘价高于均线的百分比 —— 超过则判强上涨
        strong_down_threshold: 收盘价低于均线的百分比 —— 超过则判强下跌
        max_hold_bars: 最长持有天数，None 表示不限制
            趋势过滤器允许无限期持有，避免「趋势永不破坏导致永不卖出」
    """

    ma_period: int = 60
    strong_up_threshold: float = 0.08  # 高于 MA60 8% 判强上涨
    strong_down_threshold: float = 0.08  # 低于 MA60 8% 判强下跌
    max_hold_bars: int | None = None

    def describe(self) -> str:
        hold = "无限制" if self.max_hold_bars is None else f"{self.max_hold_bars}天"
        return (
            f"TrendFilterConfig(MA{self.ma_period}, "
            f"强涨>{self.strong_up_threshold:.0%}, "
            f"强跌>{self.strong_down_threshold:.0%}, "
            f"最长持有={hold})"
        )


class TrendFilter:
    """趋势过滤器

    使用示例::

        tf = TrendFilter()
        state = tf.judge(df)                  # STRONG_UP
        if tf.should_block(action, df, bars_held):
            return ActionType.HOLD            # 拦截逆势操作
    """

    def __init__(self, config: TrendFilterConfig | None = None):
        self.config = config or TrendFilterConfig()

    # ==================== 趋势判定 ====================

    def judge(self, df: pd.DataFrame) -> TrendState:
        """判定当前趋势状态

        Args:
            df: 含 close 列的行情数据(需含当前及之前所有数据)

        Returns:
            趋势状态
        """
        if df is None or "close" not in df.columns:
            return TrendState.UNKNOWN

        close = df["close"]
        n = len(close)
        period = self.config.ma_period
        if n < max(20, period // 2):
            return TrendState.UNKNOWN

        ma = self._ma(df, period)
        if ma is None or ma <= 0:
            return TrendState.UNKNOWN

        deviation = (float(close.iloc[-1]) - ma) / ma

        if deviation >= self.config.strong_up_threshold:
            return TrendState.STRONG_UP
        if deviation <= -self.config.strong_down_threshold:
            return TrendState.STRONG_DOWN
        if deviation > 0.02:
            return TrendState.MILD_UP
        if deviation < -0.02:
            return TrendState.MILD_DOWN
        return TrendState.SIDEWAYS

    def _ma(self, df: pd.DataFrame, period: int) -> float | None:
        """取均线末值（数据不足则取可用长度的均值）"""
        if "ma" not in df.columns and f"ma{period}" in df.columns:
            col = df[f"ma{period}"].dropna()
            return float(col.iloc[-1]) if not col.empty else None
        series = df["close"].rolling(window=period, min_periods=1).mean()
        return float(series.iloc[-1])

    def deviation_pct(self, df: pd.DataFrame) -> float:
        """收盘价相对均线的偏离度（正=高于均线）"""
        if df is None or "close" not in df.columns or len(df) < 5:
            return 0.0
        ma = self._ma(df, self.config.ma_period)
        if not ma or ma <= 0:
            return 0.0
        return (float(df["close"].iloc[-1]) - ma) / ma

    # ==================== 拦截判定 ====================

    def should_block(self, side: str, df: pd.DataFrame, bars_held: int | None = None) -> bool:
        """是否应拦截该操作

        Args:
            side: "buy" 或 "sell"
            df: 行情数据
            bars_held: 已持有交易日数（用于最长持有豁免）

        Returns:
            True 表示应拦截（转为 HOLD）
        """
        state = self.judge(df)

        # 已超过最长持有期: 无条件放行，避免趋势永不破坏导致永不卖出
        if (
            bars_held is not None
            and self.config.max_hold_bars is not None
            and bars_held >= self.config.max_hold_bars
        ):
            return False

        if state == TrendState.STRONG_UP:
            return side == "sell"
        if state == TrendState.STRONG_DOWN:
            return side == "buy"
        return False

    def explain(self, df: pd.DataFrame, bars_held: int | None = None) -> str:
        """生成可读的过滤说明（用于日志与前端展示）"""
        state = self.judge(df)
        dev = self.deviation_pct(df)
        hold = "-" if bars_held is None else f"{bars_held}天"
        return f"{state.value} 偏离MA{self.config.ma_period}{dev:+.2%} 持有{hold}"
