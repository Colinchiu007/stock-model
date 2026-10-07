"""纯 pandas 技术指标实现

存在的理由
----------
项目的 ``analysis/technical.py`` 依赖 ``pandas-ta`` 计算 MACD / RSI / BOLL / ATR。
但 ``pandas-ta`` 只能在 Python >= 3.12 上安装，而本项目声明支持 Python >= 3.10。

后果（修复前）：在 3.10 / 3.11 环境下，technical.py 的
``rsi_result = None`` 分支会**静默返回原始 df** —— 指标列根本不产生，
信号生成器读不到 MACD/RSI/BOLL，于是对所有股票返回 hold。
不报错、不告警，策略实质失效。

本模块用纯 pandas/numpy 实现这些指标，使 3.10+ 全环境可用，且与
``pandas-ta`` 的**列名完全一致**，从而：
  - 有 pandas-ta 时仍优先用它（行为不变）
  - 没有时走这里的兜底，列名不漂移，上游代码零改动

列名规格（与 pandas-ta 对齐）:
  MACD_{fast}_{slow}_{signal}  DIF 线
  MACDs_{fast}_{slow}_{signal} 信号线 DEA
  MACDh_{fast}_{slow}_{signal} 柱状图
  RSI_{period}
  BBL_{period}_{std}  下轨
  BBM_{period}_{std}  中轨
  BBU_{period}_{std}  上轨
  ATR_{period}
  OBV

公式口径
--------
指标定义在业界有多种实现，pandas-ta 采用的是 Wilder 平滑口径
（如 RSI 的 Wilder 平均）。本模块对齐该口径，保证数值可与主流看盘软件比对。
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _ema(series: pd.Series, span: int) -> pd.Series:
    """指数移动平均（pandas-ta 使用 adjust=False 的 pandas 默认实现）"""
    return series.ewm(span=span, adjust=False).mean()


def macd(
    df: pd.DataFrame,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> pd.DataFrame:
    """计算 MACD

    DIF   = EMA(close, fast) - EMA(close, slow)
    DEA   = EMA(DIF, signal)
    MACD  = 2 * (DIF - DEA)     # 国内看盘软件常用 MACD 柱 = 2*(DIF-DEA)

    Args:
        df: 需含 close 列
        fast: 快线周期
        slow: 慢线周期
        signal: 信号线周期

    Returns:
        添加 MACD_{fast}_{slow}_{signal} / MACDs_ / MACDh_ 列的 DataFrame
    """
    result = df.copy()
    ema_fast = _ema(result["close"], fast)
    ema_slow = _ema(result["close"], slow)

    dif = ema_fast - ema_slow
    dea = _ema(dif, signal)
    hist = (dif - dea) * 2

    suffix = f"{fast}_{slow}_{signal}"
    result[f"MACD_{suffix}"] = dif
    result[f"MACDs_{suffix}"] = dea
    result[f"MACDh_{suffix}"] = hist
    return result


def rsi(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """计算 RSI（相对强弱指标）

    采用 Wilder 平滑口径，与 pandas-ta / 主流量���软件一致：
        涨跌幅 = close.diff()
        上涨幅 = gain.clip(lower=0)
        下跌幅 = -loss.clip(upper=0)
        平均涨幅 = Wilder 平滑后的上涨幅均值
        平均跌幅 = Wilder 平滑后的下跌幅均值
        RSI = 100 - 100 / (1 + 平均涨幅 / 平均跌幅)

    Args:
        df: 需含 close 列
        period: RSI 周期

    Returns:
        添加 RSI_{period} 列的 DataFrame（取值 0-100）
    """
    result = df.copy()
    delta = result["close"].diff()

    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)

    # Wilder 平滑等价于 alpha = 1/period 的指数平滑
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()

    rs = avg_gain / avg_loss
    # avg_loss == 0 时 RSI 定义为 100（全涨），需特判避免 0/0
    rsi_values = 100.0 - (100.0 / (1.0 + rs))
    rsi_values = rsi_values.where(avg_loss != 0, 100.0)
    # avg_gain 也为 0（横盘）时定义为 50
    rsi_values = rsi_values.where(~((avg_gain == 0) & (avg_loss == 0)), 50.0)

    result[f"RSI_{period}"] = rsi_values
    return result


def bollinger(
    df: pd.DataFrame,
    period: int = 20,
    std_dev: float = 2.0,
) -> pd.DataFrame:
    """计算布林带

    中轨 = MA(close, period)
    上轨 = 中轨 + std_dev * STD(close, period)
    下轨 = 中轨 - std_dev * STD(close, period)

    Args:
        df: 需含 close 列
        period: 均线周期
        std_dev: 标准差倍数

    Returns:
        添加 BBL_/BBM_/BBU_ 列的 DataFrame
    """
    result = df.copy()
    ma = result["close"].rolling(window=period).mean()
    std = result["close"].rolling(window=period).std()

    suffix = f"{period}_{std_dev}"
    result[f"BBM_{suffix}"] = ma
    result[f"BBU_{suffix}"] = ma + std_dev * std
    result[f"BBL_{suffix}"] = ma - std_dev * std
    return result


def atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """计算 ATR（平均真实波幅）

    真实波幅 TR = max(high-low, |high-prev_close|, |low-prev_close|)
    ATR = Wilder 平滑后的 TR 均值

    Args:
        df: 需含 high/low/close 列
        period: ATR 周期

    Returns:
        添加 ATR_{period} 列的 DataFrame
    """
    result = df.copy()
    high = result["high"]
    low = result["low"]
    close = result["close"]

    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    result[f"ATR_{period}"] = tr.ewm(alpha=1.0 / period, adjust=False).mean()
    return result


def obv(df: pd.DataFrame) -> pd.DataFrame:
    """计算 OBV（能量潮）

    收涨则累加成交量，收跌则累减成交量

    Args:
        df: 需含 close/volume 列

    Returns:
        添加 OBV 列的 DataFrame
    """
    result = df.copy()
    direction = np.sign(result["close"].diff()).fillna(0.0)
    result["OBV"] = (direction * result["volume"]).cumsum()
    return result


def kdj(
    df: pd.DataFrame,
    n: int = 9,
    m1: int = 3,
    m2: int = 3,
) -> pd.DataFrame:
    """计算 KDJ 随机指标

    RSV = (close - LLV(low, n)) / (HHV(high, n) - LLV(low, n)) * 100
    K   = SMA(RSV, m1)   （中国券商惯用 SMA(X, N) = EMA(alpha=1/N)）
    D   = SMA(K, m2)
    J   = 3K - 2D

    Args:
        df: 需含 high/low/close 列
        n: RSV 周期
        m1: K 平滑周期
        m2: D 平滑周期

    Returns:
        添加 KDJ_K/KDJ_D/KDJ_J 列的 DataFrame
    """
    result = df.copy()
    low_min = result["low"].rolling(window=n, min_periods=1).min()
    high_max = result["high"].rolling(window=n, min_periods=1).max()

    # 分母为 0（区间内最高=最低）时 RSV 定义为 50，避免除零
    span = high_max - low_min
    rsv = ((result["close"] - low_min) / span.replace(0, np.nan)).fillna(50.0) * 100

    k = rsv.ewm(alpha=1.0 / m1, adjust=False).mean()
    d = k.ewm(alpha=1.0 / m2, adjust=False).mean()

    result["KDJ_K"] = k
    result["KDJ_D"] = d
    result["KDJ_J"] = 3 * k - 2 * d
    return result


def analyze_all(
    df: pd.DataFrame,
    macd_params: tuple[int, int, int] = (12, 26, 9),
    rsi_period: int = 14,
    boll_period: int = 20,
    boll_std: float = 2.0,
    atr_period: int = 14,
    kdj_n: int = 9,
) -> pd.DataFrame:
    """一次性计算全部指标

    Args:
        df: 行情数据，需含 high/low/close/volume
        macd_params: (fast, slow, signal)
        rsi_period: RSI 周期
        boll_period: 布林带周期
        boll_std: 布林带标准差倍数
        atr_period: ATR 周期
        kdj_n: KDJ 的 N

    Returns:
        含全部指标列的 DataFrame
    """
    result = df.copy()
    required = {"high", "low", "close"}
    missing = required - set(result.columns)
    if missing:
        raise ValueError(f"行情数据缺少必需列: {sorted(missing)}")

    result = macd(result, *macd_params)
    result = rsi(result, rsi_period)
    result = bollinger(result, boll_period, boll_std)
    result = atr(result, atr_period)
    result = kdj(result, kdj_n)
    if "volume" in result.columns:
        result = obv(result)
    return result
