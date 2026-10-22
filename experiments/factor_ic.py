"""因子 IC/IR 分析(研究方案 §5: 最小验证, 用数据决定做哪些因子)

为什么先做这个
--------------
研究方案列了 12 个候选因子, 但**照文章抄是危险的**(本项目 TrendFilter
就是"看起来对、实测负收益")。所以先用零依赖、零风险的方式验证:
  这些因子在真实数据上到底有没有预测力?

指标
----
  IC (Information Coefficient):  因子值与未来收益的**横截面相关系数**
      每个调仓日算一次 Spearman 相关, 再取时间序列
      |IC| 均值 > 0.02 通常认为有弱预测力; > 0.05 算不错
  IR (Information Ratio):        IC 均值 / IC 标准差, 衡量**稳定性**
      IR > 0.3 通常认为可用

用法: python experiments/factor_ic.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from loguru import logger

logger.remove()
logger.add(sys.stderr, level="WARNING")

from stock_model.analysis.factors import default_registry
from stock_model.data.fetcher import StockDataFetcher

# 与模拟盘同源: 同样的标的池(便于后续直接用于 Phase A 筛选)
SYMBOLS = ["000002", "000001", "600036"]
START = "20240101"
FORWARD_DAYS = 5  # 预测未来 5 日收益
MIN_CROSS_SECTION = 3  # 截面至少几只才相关系数有意义


def build_panel(symbols: list[str]) -> dict[str, pd.DataFrame]:
    """取数并算因子 + 未来收益"""
    f = StockDataFetcher(source="baostock")
    reg = default_registry()
    panel = {}
    for sym in symbols:
        df = f.get_daily(sym, start_date=START)
        if df is None or df.empty:
            print(f"  {sym}: 无数据, 跳过")
            continue
        fac = reg.compute(df, reg.list())
        fac["fwd_ret"] = df["close"].shift(-FORWARD_DAYS) / df["close"] - 1.0
        panel[sym] = fac
        print(f"  {sym}: {len(df)} 根 bar, {len(reg.list())} 个因子")
    return panel


def ic_series(panel: dict[str, pd.DataFrame], factor: str) -> pd.Series:
    """逐日横截面 IC(Spearman 相关)"""
    rows = {}
    # 以第一只的索引为准(同源同窗口, 索引一致)
    base_idx = next(iter(panel.values())).index
    for date in base_idx:
        xs, ys = [], []
        for df in panel.values():
            if date not in df.index:
                continue
            x = df.loc[date, factor]
            y = df.loc[date, "fwd_ret"]
            if pd.notna(x) and pd.notna(y):
                xs.append(float(x))
                ys.append(float(y))
        if len(xs) >= MIN_CROSS_SECTION and len(set(xs)) > 1:
            rows[date] = pd.Series(xs).corr(pd.Series(ys), method="spearman")
    return pd.Series(rows).dropna()


def main() -> int:
    print("=== 因子 IC/IR 分析 ===")
    print(f"标的: {SYMBOLS} | 起始 {START} | 预测未来 {FORWARD_DAYS} 日收益\n")

    panel = build_panel(SYMBOLS)
    if len(panel) < MIN_CROSS_SECTION:
        print("有效标的不足, 无法做截面分析")
        return 1

    reg = default_registry()
    results = []
    for factor in reg.list():
        ic = ic_series(panel, factor)
        if len(ic) < 30:
            results.append((factor, float("nan"), float("nan"), len(ic)))
            continue
        ic_mean = float(ic.mean())
        ic_std = float(ic.std())
        ir = ic_mean / ic_std if ic_std and not np.isnan(ic_std) and ic_std > 0 else float("nan")
        # IC > 0 的占比: 方向一致性
        pos_rate = float((ic > 0).mean())
        results.append((factor, ic_mean, ir, len(ic)))
        _ = pos_rate

    print("\n=== 结果(按 |IC| 降序) ===")
    print(f"{'因子':<16}{'IC均值':>10}{'IR':>10}{'样本天数':>10}   判读")
    print("-" * 62)
    rows = sorted(results, key=lambda r: abs(r[1]) if not np.isnan(r[1]) else -1, reverse=True)
    for name, icm, ir, n in rows:
        if np.isnan(icm):
            verdict = "样本不足"
        elif abs(icm) >= 0.05:
            verdict = "★ 有预测力"
        elif abs(icm) >= 0.02:
            verdict = "○ 弱预测力"
        else:
            verdict = "× 基本无效"
        ic_s = f"{icm:+.4f}" if not np.isnan(icm) else "n/a"
        ir_s = f"{ir:+.3f}" if not np.isnan(ir) else "n/a"
        print(f"{name:<16}{ic_s:>10}{ir_s:>10}{n:>10}   {verdict}")

    print("\n=== 判读说明 ===")
    print("  |IC| >= 0.05  → 有预测力, 建议进 Phase A 第一批")
    print("  |IC| 0.02~0.05 → 弱, 可作为辅助因子")
    print("  |IC| <  0.02  → 基本无效, 不要为它花工程量")
    print("\n  ⚠️ 本次仅 3 只标的, 截面相关系数统计意义有限;")
    print("     结论用于**排序筛选**, 不用于直接启用策略。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
