"""因子 IC / IR 显著性分析(研究方案 Phase A)

为什么先做这个而不是直接做 UI
-----------------------------
第一版只用了 3 只标的: 截面 Spearman 相关在 n=3 时波动极大,
**统计意义有限**。照那个结论选因子 = 拿噪声当信号。
本轮扩到 20+ 只, 并补上统计检验, 让结论站得住, 再花工程量做 UI。

方法论要点
----------
1. **样本量门槛**: 有效标的 < min_symbols 时拒绝出结论, 不静默降级
2. **显著性**: 报告 t 统计量与 p 值 —— n 大时很小的 IC 也可能显著,
   只看 IC 均值会误判
3. **多重检验校正**(Bonferroni): 同时检验 13 个因子, 不校正的话
   "最显著的那个"很可能只是运气
4. **反向验证**: 随机游走数据上不该出现显著因子 —— 若出现,
   说明方法有未来函数或过拟合(有测试锁住)

判读:
    |IC| >= 0.05 且 p_corrected < 0.05  → 有预测力
    |IC| 0.02~0.05 且显著              → 弱, 可作辅助
    否则                                → 不要为它花工程量

用法:
    python experiments/factor_ic.py
    python experiments/factor_ic.py --symbols 000002,000001,600036 --min-symbols 5
"""

from __future__ import annotations

import argparse
import sys
from math import erfc, sqrt
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from loguru import logger

logger.remove()
logger.add(sys.stderr, level="WARNING")

from stock_model.analysis.factors import default_registry
from stock_model.data.fetcher import StockDataFetcher

DEFAULT_SYMBOLS = [
    "000002", "000001", "600036", "000651", "600519",
    "600000", "600016", "600030", "000858", "000333",
    "601318", "601899", "002415", "002594", "300750",
    "600276", "000725", "601012", "600887", "603288",
    "000568", "601166", "600309", "002304",
]
FORWARD_DAYS = 5
MIN_CROSS_SECTION = 3


def add_forward_return(
    df: pd.DataFrame, close: pd.Series, days: int
) -> pd.DataFrame:
    """加上未来 N 日收益列

    Args:
        df: 因子 DataFrame(不含 close, 索引与 close 对齐)
        close: 原始收盘价序列
        days: 前瞻天数

    严格用 ``shift(-days)``(未来函数检测: 用 shift(+days) 会变成
    "用过去预测过去", 有测试锁住)。
    """
    out = df.copy()
    out["fwd_ret"] = close.shift(-days) / close - 1.0
    return out


def build_panel(symbols: list[str], start: str):
    """取数 + 算因子 + 未来收益

    Returns:
        (panel, closes): panel 是 {symbol: 因子DataFrame(含 fwd_ret)},
        closes 是 {symbol: close 序列} —— 扫描时要按各自的 horizon 重算
        fwd_ret, 故必须把 close 单独带出来(因子面板里没有 close 列)。
    """
    f = StockDataFetcher(source="baostock")
    reg = default_registry()
    names = reg.list()
    panel: dict[str, pd.DataFrame] = {}
    closes: dict[str, pd.Series] = {}
    for sym in symbols:
        try:
            df = f.get_daily(sym, start_date=start)
        except Exception as e:
            print(f"  {sym}: 取数失败({type(e).__name__}), 跳过")
            continue
        if df is None or df.empty:
            print(f"  {sym}: 无数据, 跳过")
            continue
        fac = reg.compute(df, names)
        # 注意: fac 不含 close(只有因子列), 未来收益要基于**原始行情**
        fac = add_forward_return(fac, df["close"], FORWARD_DAYS)
        panel[sym] = fac
        closes[sym] = df["close"]
    return panel, closes


def ic_series(panel: dict[str, pd.DataFrame], factor: str) -> pd.Series:
    """逐日横截面 IC(Spearman 相关)"""
    base_idx = next(iter(panel.values())).index
    rows: dict = {}
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


def _effective_sample_size(ic: pd.Series, max_lag: int | None = None) -> float:
    """Newey-West 折减后的有效样本量

    IC 序列存在正自相关时, 独立观测数少于名义天数。用 Bartlett 核
    估计自相关并折减:

        n_eff = n / (1 + 2 * Σ_{l=1}^{L} (1 - l/(L+1)) * ρ_l)

    ρ_l <= 0 的项不累加(负自相关不会放大 t), 保守起见只累加正的自相关。
    结果夹在 [2, n] 之间。
    """
    n = len(ic)
    if n < 10:
        return max(2.0, float(n))
    if max_lag is None:
        # 常用经验: 预测期 5 日时, 取 4*sqrt(n) 或 5 日重叠 -> 用 2*FORWARD_DAYS
        max_lag = min(int(4 * np.sqrt(n)), max(2 * FORWARD_DAYS, 5))
    x = ic.to_numpy(dtype=float)
    x = x - x.mean()
    denom = float(np.dot(x, x))
    if denom <= 0:
        return max(2.0, float(n))

    acc = 0.0
    for lag in range(1, max_lag + 1):
        rho = float(np.dot(x[:-lag], x[lag:])) / denom
        if rho > 0:
            acc += (1.0 - lag / (max_lag + 1.0)) * rho

    n_eff = n / (1.0 + 2.0 * acc)
    return float(min(max(n_eff, 2.0), float(n)))


def split_segments(
    index: pd.Index, n_segments: int = 3
) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """把样本区间切成 n 段, 边界落在**真实交易日**上

    Returns:
        [(start, end), ...]; 数据不足以分段时返回空列表(不硬凑)
    """
    n = len(index)
    if n_segments < 1 or n < n_segments * 10:
        # 每段至少 10 根, 否则段内 IC 无意义
        return []
    bounds: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    for k in range(n_segments):
        lo = int(n * k / n_segments)
        hi = int(n * (k + 1) / n_segments) - 1
        if hi <= lo:
            continue
        bounds.append((index[lo], index[hi]))
    return bounds


def analyze_by_segment(
    panel: dict[str, pd.DataFrame],
    n_segments: int = 3,
    min_days: int = 60,
    min_symbols: int = 10,
    alpha: float = 0.05,
) -> list[dict]:
    """分段稳健性分析

    对每个因子: 切成 n_segments 段, 每段独立算 IC / t / p(带 Newey-West),
    再做 Bonferroni 校正; 然后判稳健性:
      - ``consistent_direction``: 各有效段的 IC 符号是否一致
        (牛段为正、熊段为负 = 不稳定, 不能用于建仓)
      - ``robust``: 方向一致 **且** 显著段数达标

    为什么要分段(研究方案 §5.4):
      单一时间窗口显著的因子, 可能只是那段行情的巧合。若换个环境就失效,
      基于它建策略 = 过拟合于一段历史。本项目 TrendFilter 已吃过这个亏
      (上涨市实测恶化 -2.36pp)。
    """
    if len(panel) < min_symbols:
        raise ValueError(f"样本不足: 有效标的 {len(panel)} < 门槛 {min_symbols}")

    base_idx = next(iter(panel.values())).index
    segs = split_segments(base_idx, n_segments)
    if not segs:
        raise ValueError(f"数据不足以分 {n_segments} 段(需每段至少 10 根)")

    reg = default_registry()
    n_tests = max(1, len(reg.list()) * len(segs))
    rows: list[dict] = []
    for factor in reg.list():
        seg_ics: list[float] = []
        sig_count = 0
        insufficient = 0
        for start, end in segs:
            sub = {k: v.loc[start:end] for k, v in panel.items()}
            sub = {k: v for k, v in sub.items() if len(v) > 0}
            ic = ic_series(sub, factor)
            if len(ic) < min_days:
                insufficient += 1
                continue
            ic_mean = float(ic.mean())
            ic_std = float(ic.std(ddof=1))
            seg_ics.append(ic_mean)
            if ic_std > 0 and not np.isnan(ic_std):
                eff_n = _effective_sample_size(ic)
                t = ic_mean / (ic_std / np.sqrt(max(eff_n, 2.0)))
                p = erfc(abs(t) / sqrt(2)) * n_tests  # Bonferroni
                if p < alpha:
                    sig_count += 1

        valid = [x for x in seg_ics if not np.isnan(x)]
        if len(valid) >= 2:
            same_sign = all(x > 0 for x in valid) or all(x < 0 for x in valid)
        elif len(valid) == 1:
            same_sign = True
        else:
            same_sign = False

        need_sig = 2 if len(segs) >= 2 else 1
        robust = bool(same_sign and sig_count >= need_sig and len(valid) >= 1)

        rows.append({
            "factor": factor,
            "n_segments": len(segs),
            "n_significant": sig_count,
            "n_insufficient": insufficient,
            "ic_by_segment": seg_ics,
            "ic_mean_all": float(np.mean(valid)) if valid else float("nan"),
            "consistent_direction": same_sign,
            "robust": robust,
            "n_days": len(valid),
            "status": "insufficient" if insufficient >= len(segs) / 2 else "ok",
        })
    return rows


def analyze(panel: dict[str, pd.DataFrame], min_symbols: int = 10) -> list[dict]:
    """算每个因子的 IC / IR / t / p / 校正后 p

    Raises:
        ValueError: 有效标的数量不足 min_symbols(拒绝出结论)
    """
    if len(panel) < min_symbols:
        raise ValueError(
            f"样本不足: 有效标的 {len(panel)} < 门槛 {min_symbols}。"
            f"截面相关系数在小样本下波动极大, 不据此出结论。"
        )

    reg = default_registry()
    rows: list[dict] = []
    for factor in reg.list():
        ic = ic_series(panel, factor)
        n = len(ic)
        if n < 30:
            rows.append({
                "factor": factor, "ic_mean": float("nan"), "ir": float("nan"),
                "t_stat": float("nan"), "p_value": float("nan"),
                "p_corrected": float("nan"), "n_days": n,
            })
            continue
        ic_mean = float(ic.mean())
        ic_std = float(ic.std(ddof=1))
        if ic_std > 0 and not np.isnan(ic_std):
            ir = ic_mean / ic_std
            # ⚠️ 不能直接用 sqrt(n) 算 t: 连续交易日的截面 IC **高度自相关**
            # (同一批标的在同一段行情里), 独立样本量远小于 n, 直接用会
            # 系统性高估显著性 -> 假阳性。实测: 随机游走数据上 mom_10 /
            # bb_position 被误判为"显著"(校正后 p=0.031/0.012)。
            # 故用 Newey-West 调整: 按自相关折减有效样本量。
            eff_n = _effective_sample_size(ic)
            t_stat = ic_mean / (ic_std / np.sqrt(max(eff_n, 2.0)))
            p_value = erfc(abs(t_stat) / sqrt(2))
        else:
            ir = float("nan")
            t_stat = float("nan")
            p_value = float("nan")
        rows.append({
            "factor": factor, "ic_mean": ic_mean, "ir": ir,
            "t_stat": t_stat, "p_value": p_value,
            "p_corrected": float("nan"),  # 下面统一校正
            "n_days": n,
        })

    # Bonferroni 多重检验校正: 同时检验 k 个因子, 阈值收紧 k 倍
    k = len([r for r in rows if not np.isnan(r["p_value"])])
    for r in rows:
        if np.isnan(r["p_value"]):
            continue
        r["p_corrected"] = min(1.0, r["p_value"] * k)

    return rows


def build_grid(horizons: tuple[int, ...], starts: tuple[str, ...]) -> list[dict]:
    """构造参数扫描网格: 预测期 × 时间起点"""
    if not horizons or not starts:
        return []
    return [{"horizon": h, "start": s} for s in starts for h in horizons]


def scan_and_summarize(
    panel: dict[str, pd.DataFrame],
    horizons: tuple[int, ...] = (1, 5, 10, 20),
    starts: tuple[str, ...] = ("20240101", "20250101"),
    min_combos: int = 4,
    min_days: int = 60,
    min_symbols: int = 10,
    alpha: float = 0.05,
    robust_ratio: float = 0.5,
    recompute_fwd: bool = True,
    closes: dict[str, pd.Series] | None = None,
) -> list[dict]:
    """多预测期 / 多起点扫描, 汇总每个因子的稳健性

    为什么(研究方案 §5.6):
      上轮「无一稳健」结论受限于单一预测期(5 日)与单一起点。
      若某因子只在特定预测期有效, 只测一个就会**假阴性** ——
      假阴性与假阳性同样危险, 都会导向错误的工程决策。

    Args:
        panel: 因子面板
        recompute_fwd: 是否按每个 horizon **重算**前瞻收益。
            默认 True —— 曾有 bug: 用面板里固定的 5 日前瞻去扫 1/5/10/20 日,
            导致各 horizon 的 IC 完全相同, 扫描维度形同虚设。
        closes: 各标的的 close 序列; recompute_fwd=True 时必需
            (面板本身不含 close, 见 build_panel 的设计)
    """
    combos = build_grid(horizons, starts)
    if len(combos) < min_combos:
        raise ValueError(
            f"组合数不足: {len(combos)} < 门槛 {min_combos}。参数空间太小, 结论不可靠。"
        )
    if len(panel) < min_symbols:
        raise ValueError(f"样本不足: 有效标的 {len(panel)} < 门槛 {min_symbols}")
    if recompute_fwd and not closes:
        raise ValueError("recompute_fwd=True 需要 closes(各标的 close 序列)")

    reg = default_registry()
    n_tests = max(1, len(reg.list()) * len(combos))
    rows: list[dict] = []
    for factor in reg.list():
        n_sig = 0
        ics: list[float] = []
        for combo in combos:
            start = pd.Timestamp(combo["start"])
            h = int(combo["horizon"])
            sub: dict[str, pd.DataFrame] = {}
            for k, v in panel.items():
                w = v.loc[v.index >= start]
                if len(w) < min_days:
                    continue
                if recompute_fwd and closes and k in closes:
                    c = closes[k]
                    w = w.copy()
                    # 按**本组合的 horizon** 重算前瞻收益(关键修复)
                    w["fwd_ret"] = c.shift(-h) / c - 1.0
                sub[k] = w
            if len(sub) < min_symbols:
                continue
            ic = ic_series(sub, factor)
            if len(ic) < min_days:
                continue
            ic_mean = float(ic.mean())
            ic_std = float(ic.std(ddof=1))
            ics.append(ic_mean)
            if ic_std > 0 and not np.isnan(ic_std):
                eff_n = _effective_sample_size(ic)
                t = ic_mean / (ic_std / np.sqrt(max(eff_n, 2.0)))
                p = erfc(abs(t) / sqrt(2)) * n_tests  # Bonferroni
                if p < alpha:
                    n_sig += 1

        valid = [x for x in ics if not np.isnan(x)]
        if len(valid) >= 2:
            same = all(x > 0 for x in valid) or all(x < 0 for x in valid)
        elif len(valid) == 1:
            same = True
        else:
            same = False

        rows.append({
            "factor": factor,
            "n_combos": len(combos),
            "n_significant": n_sig,
            "n_consistent": len(valid) if same else 0,
            "ic_by_combo": ics,
            "ic_mean_all": float(np.mean(valid)) if valid else float("nan"),
            "consistent_direction": same,
            "robust": bool(same and n_sig >= max(1, int(len(combos) * robust_ratio))),
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="因子 IC 显著性分析")
    ap.add_argument("--symbols", default="", help="逗号分隔(默认内置 24 只)")
    ap.add_argument("--start", default="20240101")
    ap.add_argument("--min-symbols", type=int, default=10, help="样本量门槛")
    ap.add_argument(
        "--segments",
        type=int,
        default=0,
        help="分段稳健性分析: 切成 N 段各自检验(0=不做, 默认 0)",
    )
    ap.add_argument(
        "--scan",
        action="store_true",
        help="参数扫描: 多预测期 × 多起点(验证'无 alpha'是否为假阴性)",
    )
    ap.add_argument(
        "--horizons",
        default="1,5,10,20",
        help="扫描用预测期(逗号分隔)",
    )
    ap.add_argument(
        "--scan-starts",
        default="20240101,20250101",
        help="扫描用时间起点(逗号分隔)",
    )
    args = ap.parse_args()

    symbols = (
        [s.strip() for s in args.symbols.split(",") if s.strip()]
        or DEFAULT_SYMBOLS
    )

    print("=== 因子 IC 显著性分析 ===")
    print(f"标的 {len(symbols)} 只 | 起始 {args.start} | 预测未来 {FORWARD_DAYS} 日")
    print(f"样本量门槛: {args.min_symbols}\n")

    panel, closes = build_panel(symbols, args.start)
    print(f"有效标的: {len(panel)}/{len(symbols)}")
    print(f"因子数  : {len(default_registry().list())}\n")

    try:
        rows = analyze(panel, min_symbols=args.min_symbols)
    except ValueError as e:
        print(f"拒绝出结论: {e}")
        return 1

    def fmt(v, width: int, prec: int = 4) -> str:
        if isinstance(v, float) and np.isnan(v):
            return "n/a".rjust(width)
        return f"{v:+.{prec}f}".rjust(width)

    if args.scan:
        horizons = tuple(int(x) for x in args.horizons.split(",") if x.strip())
        starts = tuple(s.strip() for s in args.scan_starts.split(",") if s.strip())
        print(f"=== 参数扫描(预测期 {horizons} × 起点 {starts}) ===\n")
        try:
            scan_rows = scan_and_summarize(
                panel, horizons=horizons, starts=starts,
                min_symbols=args.min_symbols,
                recompute_fwd=True, closes=closes,
            )
        except ValueError as e:
            print(f"拒绝出结论: {e}")
            return 1
        head = f"{'因子':<16}{'各组合IC':<38}{'显著':>6}{'一致':>6}{'稳健':>7}"
        print(head)
        print("-" * len(head))
        for r in sorted(
            scan_rows,
            key=lambda x: (x["robust"],
                           abs(x["ic_mean_all"]) if not np.isnan(x["ic_mean_all"]) else 0),
            reverse=True,
        ):
            ics_s = " ".join(f"{x:+.3f}" for x in r["ic_by_combo"]) or "n/a"
            print(
                f"{r['factor']:<16}{ics_s:<38}{r['n_significant']:>6}"
                f"{'是' if r['consistent_direction'] else '否':>6}"
                f"{'★' if r['robust'] else '×':>7}"
            )
        print(f"\n  稳健判据: 方向一致 且 显著组合数 >= 一半({len(horizons) * len(starts) // 2})")
        print("  目的: 排除'只测单一预测期导致的假阴性'。\n")

    if args.segments > 0:
        print(f"=== 分段稳健性({args.segments} 段) ===\n")
        try:
            seg_rows = analyze_by_segment(
                panel, n_segments=args.segments, min_symbols=args.min_symbols
            )
        except ValueError as e:
            print(f"拒绝出结论: {e}")
            return 1
        head = f"{'因子':<16}{'各段IC':<34}{'显著段':>7}{'方向一致':>9}{'稳健':>7}"
        print(head)
        print("-" * len(head))
        for r in sorted(
            seg_rows,
            key=lambda x: (x["robust"], abs(x["ic_mean_all"])
                           if not np.isnan(x["ic_mean_all"]) else 0),
            reverse=True,
        ):
            segs_s = " ".join(f"{x:+.3f}" for x in r["ic_by_segment"]) or "n/a"
            print(
                f"{r['factor']:<16}{segs_s:<34}{r['n_significant']:>7}"
                f"{'是' if r['consistent_direction'] else '否':>9}"
                f"{'★' if r['robust'] else '×':>7}"
            )
        print("\n  稳健判据: 方向一致 **且** 显著段数 >= 2")
        print("  单一窗口显著但分段不稳 = 过拟合于那段行情, 不可据此建仓。\n")

    print("=== 结果(按 |IC| 降序) ===")
    header = (
        f"{'因子':<16}{'IC均值':>9}{'IR':>8}{'t':>8}{'p':>9}{'p校正':>9}{'天数':>7}  判读"
    )
    print(header)
    print("-" * len(header))
    ordered = sorted(
        rows,
        key=lambda x: abs(x["ic_mean"]) if not np.isnan(x["ic_mean"]) else -1,
        reverse=True,
    )
    for r in ordered:
        icm, p_c = r["ic_mean"], r["p_corrected"]
        if np.isnan(icm):
            verdict = "样本不足"
        elif p_c < 0.05 and abs(icm) >= 0.05:
            verdict = "★ 显著且有预测力"
        elif p_c < 0.05:
            verdict = "○ 显著但弱"
        else:
            verdict = "× 不显著"
        print(
            f"{r['factor']:<16}{fmt(icm, 9)}{fmt(r['ir'], 8, 3)}"
            f"{fmt(r['t_stat'], 8, 2)}{fmt(r['p_value'], 9, 3)}"
            f"{fmt(p_c, 9, 3)}{r['n_days']:>7}  {verdict}"
        )

    print("\n=== 判读说明 ===")
    print("  p校正 < 0.05 且 |IC| >= 0.05 → 有预测力, 建议进第一批")
    print("  p校正 < 0.05 但 |IC| 小       → 显著但弱, 可作辅助")
    print("  p校正 >= 0.05                 → 不显著, 别为它花工程量")
    print(f"\n  已做 Bonferroni 校正({len(rows)} 个因子同时检验), 避免把运气当信号。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
