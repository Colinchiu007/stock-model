"""因子注册表(Phase A: 因子层抽象)

为什么需要这一层(2026-10-22 研究方案)
------------------------------------
此前"因子"就是硬编码在 ``signals.py`` 里的 4 条技术规则 ——
想试"RSI + MACD 但不要布林带"必须改代码。而后续的:
  - ML 模型(需要一组可组合的特征输入)
  - SHAP 归因(需要"每个因子贡献多少")
  - 参数优化(需要搜索空间)
全部依赖一个**可组合、可按需计算**的因子层, 故它是第一优先。

设计约束
--------
1. **纯函数**: 因子必须是 ``DataFrame -> Series`` 的确定性函数。
   同输入必同输出, 不得修改入参 —— 否则归因与回测都不可信。
2. **按需计算**: 只算勾选的因子, 未选中的不消耗 CPU
   (选股扫描 30 只 × 全量因子会明显拖慢, 实测过扫描 354s 的教训)。
3. **输出长度 == 输入长度**: 便于按索引对齐做截面分析 / IC 计算。
4. 未知因子名**显式报错**, 不静默跳过(静默跳过会让"配错名字"变成
   "因子悄悄没生效", 正是本项目反复吃亏的"看起来正常、结果是错的")。

⚠️ 本模块与 ``signals.py`` 的旧规则**并存**, 不替换。观察期内
``ManualStrategy`` 的决策规则不许动(见 HANDOVER 第三节)。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

import pandas as pd

FactorFn = Callable[[pd.DataFrame], "pd.Series[float]"]


class FactorRegistry:
    """因子注册表: 注册 / 列举 / 按需计算"""

    def __init__(self) -> None:
        self._fns: dict[str, FactorFn] = {}

    def register(self, name: str) -> Callable[[FactorFn], FactorFn]:
        """装饰器: 注册一个因子

        因子签名必须是 ``fn(df) -> Series``, 且**不得修改 df**。
        """

        def _wrap(fn: FactorFn) -> FactorFn:
            if name in self._fns:
                raise ValueError(f"因子重复注册: {name}")
            self._fns[name] = fn
            return fn

        return _wrap

    def add(self, name: str, fn: FactorFn) -> None:
        """直接注册(供非装饰器场景)"""
        if name in self._fns:
            raise ValueError(f"因子重复注册: {name}")
        self._fns[name] = fn

    def list(self) -> list[str]:
        """所有已注册因子名(注册顺序)"""
        return list(self._fns)

    def compute(self, df: pd.DataFrame, names: Iterable[str]) -> pd.DataFrame:
        """按需计算指定因子, 返回 DataFrame(列为因子名, 索引与输入一致)

        Args:
            df: 含 OHLCV 的行情
            names: 要计算的因子名列表

        Raises:
            KeyError: 未知因子名(不静默跳过)

        Returns:
            只含所选因子的 DataFrame; 长度与 ``df`` 一致
        """
        keys = list(names)
        unknown = [n for n in keys if n not in self._fns]
        if unknown:
            raise KeyError(f"未知因子: {unknown}; 已注册: {sorted(self._fns)}")

        out = pd.DataFrame(index=df.index)
        for name in keys:
            out[name] = self._fns[name](df)
        return out


def default_registry() -> FactorRegistry:
    """内置因子注册表(第一批: 动量 / 反转 / 波动 / 量价 / 技术)

    命名约定: ``<族>_<周期>``, 便于前端分组与参数网格搜索。
    """
    reg = FactorRegistry()

    # ---------- 动量 ----------
    for p in (5, 10, 20, 60):
        reg.add(f"mom_{p}", _momentum(p))

    # ---------- 反转(= 负动量) ----------
    for p in (5, 20):
        reg.add(f"rev_{p}", _reversal(p))

    # ---------- 波动 ----------
    reg.add("vol_20", _volatility(20))
    reg.add("atr_ratio_14", _atr_ratio(14))

    # ---------- 量价 ----------
    reg.add("volume_ratio", _volume_ratio(20))
    reg.add("obv_slope", _obv_slope(20))

    # ---------- 技术(来自既有指标, 复用口径) ----------
    reg.add("rsi_14", _rsi(14))
    reg.add("macd_hist", _macd_hist())
    reg.add("bb_position", _bb_position(20, 2.0))

    return reg


# ==================== 因子实现(全部是纯函数) ====================


def _momentum(period: int) -> FactorFn:
    """动量: (close / close.shift(period) - 1)"""

    def _f(df: pd.DataFrame) -> pd.Series:
        return df["close"] / df["close"].shift(period) - 1.0

    return _f


def _reversal(period: int) -> FactorFn:
    """反转: 负动量(跌得多 -> 值大, 预期反弹)"""

    def _f(df: pd.DataFrame) -> pd.Series:
        return -(df["close"] / df["close"].shift(period) - 1.0)

    return _f


def _volatility(period: int) -> FactorFn:
    """波动率: 收益率滚动标准差"""

    def _f(df: pd.DataFrame) -> pd.Series:
        return df["close"].pct_change().rolling(period).std()

    return _f


def _atr_ratio(period: int) -> FactorFn:
    """ATR / close: 归一化波动幅度"""
    from stock_model.analysis.indicators import atr

    def _f(df: pd.DataFrame) -> pd.Series:
        with_atr = atr(df.copy(), period=period)
        col = f"atr{period}"
        val = with_atr[col] if col in with_atr.columns else with_atr.iloc[:, -1]
        return pd.Series(val, index=df.index) / df["close"]

    return _f


def _volume_ratio(period: int) -> FactorFn:
    """量比: 当日量 / 过去 period 日均量"""

    def _f(df: pd.DataFrame) -> pd.Series:
        ma = df["volume"].rolling(period).mean()
        return df["volume"] / ma.replace(0, pd.NA)

    return _f


def _obv_slope(period: int) -> FactorFn:
    """OBV 斜率: 量能趋势(经 close 量纲归一)"""
    from stock_model.analysis.indicators import obv

    def _f(df: pd.DataFrame) -> pd.Series:
        with_obv = obv(df.copy())
        col = "obv"
        val = with_obv[col] if col in with_obv.columns else with_obv.iloc[:, -1]
        s = pd.Series(val, index=df.index).astype(float)
        # 用 diff 的滚动均值 / 成交量均值, 得到无量纲斜率
        return s.diff().rolling(period).mean() / df["volume"].rolling(period).mean()

    return _f


def _rsi(period: int) -> FactorFn:
    """RSI(复用 indicators.rsi, 保证与信号层同一口径)"""
    from stock_model.analysis.indicators import rsi as _rsi_ind

    def _f(df: pd.DataFrame) -> pd.Series:
        out = _rsi_ind(df.copy(), period=period)
        col = f"RSI_{period}"
        val = out[col] if col in out.columns else out.iloc[:, -1]
        return pd.Series(val, index=df.index)

    return _f


def _macd_hist() -> FactorFn:
    """MACD 柱(复用 indicators.macd, 同一口径)"""
    from stock_model.analysis.indicators import macd as _macd_ind

    def _f(df: pd.DataFrame) -> pd.Series:
        out = _macd_ind(df.copy())
        for col in out.columns:
            if col.startswith("MACDh_"):
                return pd.Series(out[col], index=df.index)
        return pd.Series(out.iloc[:, -1], index=df.index)

    return _f


def _bb_position(period: int, std_dev: float) -> FactorFn:
    """价格在布林带内的相对位置: (close - 下轨) / (上轨 - 下轨)

    0 = 在下轨, 1 = 在上轨; 可越界(价格突破时)。
    """
    from stock_model.analysis.indicators import bollinger

    def _f(df: pd.DataFrame) -> pd.Series:
        out = bollinger(df.copy(), period=period, std_dev=std_dev)
        suffix = f"{period}_{std_dev}"
        lower = out.get(f"BBL_{suffix}")
        upper = out.get(f"BBU_{suffix}")
        if lower is None or upper is None:
            cols = [c for c in out.columns if c.startswith("BBL_")]
            lower = out[cols[0]]
            upper = out[list(out.columns).index(cols[0]) + 2]
        width = (upper - lower).replace(0, pd.NA)
        return (df["close"] - lower) / width

    return _f
