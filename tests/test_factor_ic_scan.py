"""IC 参数扫描(多预测期 / 多起点)—— 验证"无 alpha"不是样本造成的假阴性

为什么(研究方案 §5.6 优先级 2)
-----------------------------
上轮结论「13 个常规因子无一稳健」, 但**结论本身**受限于:
  - 单一预测期(5 日)
  - 单一时间起点(2024-01)
  - 24 只标的

风险: 若某因子只在特定预测期(如 20 日)有效, 只测 5 日就会漏掉 ——
这叫**假阴性**, 和假阳性一样危险(都会得出错误的工程决策)。
本项目已有两次结论反转(3只→9个★, 24只→1个, 分段→0个), 必须在
**下"无 alpha"这个终局结论前**把参数空间扫一遍。

契约:
  1. 扫描维度: 预测期(1/5/10/20) × 时间起点(多个)
  2. 每个组合独立算 IC/t/p(带 Newey-West + 分段 Bonferroni)
  3. 汇总判据: 因子在**多少个组合上显著**, 而不是只看单一组合
  4. 稳健门槛可配置, 默认要求 >= 半数组合显著(避免"碰巧一个显著")
  5. 组合数不足时拒绝出结论
  6. 预测期改变只影响 fwd_ret, 因子值本身不变(纯函数保证)
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stock_model.analysis.factors import default_registry

REPO_ROOT = Path(__file__).resolve().parents[1]


def load_ic():
    path = REPO_ROOT / "experiments" / "factor_ic.py"
    spec = importlib.util.spec_from_file_location("_factor_ic_scan", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def ic():
    return load_ic()


def _mk(n: int = 700, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2023-01-02", periods=n)
    ret = rng.normal(0.0003, 0.015, n)
    close = pd.Series(10.0 * np.cumprod(1.0 + ret, axis=0), index=idx)
    return pd.DataFrame(
        {
            "open": close * 0.99,
            "high": close * 1.02,
            "low": close * 0.98,
            "close": close,
            "volume": [1_000_000 + i * 1000 for i in range(n)],
        },
        index=idx,
    )


class TestScanGrid:
    def test_grid_size(self, ic):
        """扫描组合数 = 预测期数 × 起点数"""
        grid = ic.build_grid(horizons=(1, 5), starts=("20240101", "20250101"))
        assert len(grid) == 4

    def test_grid_contains_both_dims(self, ic):
        grid = ic.build_grid(horizons=(5,), starts=("20240101",))
        assert grid[0]["horizon"] == 5
        assert grid[0]["start"] == "20240101"

    def test_empty_dims_returns_empty(self, ic):
        assert ic.build_grid(horizons=(), starts=("20240101",)) == []
        assert ic.build_grid(horizons=(5,), starts=()) == []

    def test_horizon_actually_changes_fwd_ret(self, ic):
        """预测期必须**真的改变**前瞻收益, 不能所有 horizon 算出同一个 IC

        曾出过的 bug: scan 只按 start 切片, fwd_ret 用的是 build_panel 里
        固定的 5 日前瞻 -> 4 个 horizon 的 IC 完全相同, 扫描维度形同虚设。
        """
        # 用确定性数据: 单调上涨序列
        n = 300
        idx = pd.bdate_range("2024-01-01", periods=n)
        close = pd.Series([10.0 + i * 0.1 for i in range(n)], index=idx)
        df = pd.DataFrame(
            {
                "open": close,
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "volume": [1e6] * n,
            },
            index=idx,
        )
        r1 = ic.add_forward_return(pd.DataFrame(index=idx), close, 1)
        r20 = ic.add_forward_return(pd.DataFrame(index=idx), close, 20)
        # 上涨序列: 前瞻越远收益越大
        assert float(r20["fwd_ret"].iloc[0]) > float(r1["fwd_ret"].iloc[0])

    def test_scan_recomputes_fwd_ret_per_horizon(self, ic):
        """扫描时每个 horizon 必须用**自己的**前瞻期重算 fwd_ret"""
        reg = default_registry()
        names = reg.list()
        panel = {}
        closes = {}
        for i in range(15):
            df = _mk(400, seed=70000 + i)
            fac = reg.compute(df, names)
            # 故意放一个错的 fwd_ret(全 0): 若 scan 不重算, IC 会全 NaN/0
            fac["fwd_ret"] = 0.0
            panel[f"S{i}"] = fac
            closes[f"S{i}"] = df["close"]
        rows = ic.scan_and_summarize(
            panel,
            horizons=(1, 20),
            starts=("20240101", "20250101"),
            min_combos=2,
            recompute_fwd=True,
            closes=closes,
        )
        # 若真的重算了, 每个因子都应得到有效 IC(不是全 0 导致的 NaN)
        got = [r for r in rows if not np.isnan(r["ic_mean_all"])]
        assert got, "scan 未重算 fwd_ret: 所有因子 IC 均为 NaN(使用了面板里的常数 fwd_ret)"


class TestScanAnalysis:
    def _panel(self, n_symbols=20, n=700, seed0=0):
        """返回 (panel, closes) —— 扫描需要 close 来按 horizon 重算前瞻收益"""
        reg = default_registry()
        names = reg.list()
        out = {}
        closes = {}
        for i in range(n_symbols):
            df = _mk(n, seed=seed0 + i)
            fac = reg.compute(df, names)
            fac["fwd_ret"] = df["close"].shift(-5) / df["close"] - 1.0
            out[f"S{i}"] = fac
            closes[f"S{i}"] = df["close"]
        return out, closes

    def test_too_few_combinations_rejected(self, ic):
        """组合数不足门槛 -> 拒绝出结论"""
        panel, _closes = self._panel()
        with pytest.raises(ValueError, match="组合数不足"):
            ic.scan_and_summarize(
                panel, horizons=(5,), starts=("20240101",), min_combos=10, closes=_closes
            )

    def test_summary_contains_n_significant(self, ic):
        """汇总必须给出每个因子在多少组合上显著"""
        panel, _closes = self._panel()
        rows = ic.scan_and_summarize(
            panel, horizons=(1, 5), starts=("20240101", "20250101"), min_combos=2, closes=_closes
        )
        for r in rows:
            assert "n_combos" in r
            assert "n_significant" in r
            assert "n_consistent" in r

    def test_robust_threshold_default_is_half(self, ic):
        """默认稳健门槛 = 半数组合显著(避免"碰巧一个显著")"""
        panel, _closes = self._panel()
        rows = ic.scan_and_summarize(
            panel, horizons=(1, 5), starts=("20240101", "20250101"), min_combos=2, closes=_closes
        )
        for r in rows:
            assert r["n_significant"] <= r["n_combos"]
            if r["robust"]:
                assert r["n_significant"] >= r["n_combos"] / 2

    def test_random_data_no_robust_factor(self, ic):
        """随机游走: 多组合扫描下仍不应有稳健因子(反向验证)"""
        panel, _closes = self._panel(n_symbols=25, n=700, seed0=40000)
        rows = ic.scan_and_summarize(
            panel, horizons=(1, 5), starts=("20240101", "20250101"), min_combos=2, closes=_closes
        )
        robust = [r for r in rows if r["robust"]]
        assert not robust, f"随机数据不该有稳健因子: {[r['factor'] for r in robust]}"

    def test_strong_signal_detected_across_combos(self, ic):
        """构造在所有组合都有效的信号 -> 应判显著(证明方法能检出真信号)"""
        reg = default_registry()
        names = reg.list()
        panel = {}
        for i in range(25):
            df = _mk(700, seed=50000 + i)
            fac = reg.compute(df, names)
            rng = np.random.default_rng(60000 + i)
            fac["fwd_ret"] = fac["mom_20"] * 0.08 + rng.normal(0, 0.006, len(fac))
            panel[f"S{i}"] = fac
        # 本测的是"汇总判据"而非"重算前瞻", 故 recompute_fwd=False
        rows = ic.scan_and_summarize(
            panel, horizons=(5,), starts=("20240101", "20250101"), min_combos=2, recompute_fwd=False
        )
        mom = next(r for r in rows if r["factor"] == "mom_20")
        assert mom["n_significant"] >= 1, (
            f"强信号应在多数组合显著, 实际 {mom['n_significant']}/{mom['n_combos']}"
        )

    def test_robust_requires_direction_consistency(self, ic):
        """方向翻转绝不能判稳健(即使显著数够)

        变异验证发现: "忽略方向一致性"的变异在随机数据下抓不到
        (此时 n_sig=0, same and 0>=x 与 0>=x 同假)。故显式构造
        "显著但方向翻转"的场景。
        """
        reg = default_registry()
        names = reg.list()
        panel = {}
        n = 700
        for i in range(25):
            df = _mk(n, seed=80000 + i)
            fac = reg.compute(df, names)
            rng = np.random.default_rng(90000 + i)
            # 以 2025-01-01 为界: 之前正相关、之后负相关。
            # 两个扫描起点分别落在这两段 -> 各组合 IC 符号相反。
            boundary = pd.Timestamp("20250101")
            signal = np.where(fac.index < boundary, 1.0, -1.0)
            fac["fwd_ret"] = fac["mom_20"] * 0.15 * signal + rng.normal(0, 0.004, n)
            panel[f"S{i}"] = fac
        rows = ic.scan_and_summarize(
            panel, horizons=(5,), starts=("20240101", "20250101"), min_combos=2, recompute_fwd=False
        )
        mom = next(r for r in rows if r["factor"] == "mom_20")
        assert not mom["consistent_direction"], "构造的是方向翻转信号"
        assert not mom["robust"], "方向翻转绝不能判稳健"

    def test_robust_threshold_is_ratio_not_one(self, ic):
        """稳健门槛必须是"按比例"的, 不能退化成"任一显著"

        变异验证发现: "任一显著即稳健"的变异在随机数据下抓不到
        (n_sig=0 时两种判据同假)。故用 robust_ratio=1.0(要求全部组合显著)
        来区分: 只在部分组合有信号的因子, 在 ratio=1.0 下必须不稳健。
        """
        reg = default_registry()
        names = reg.list()
        panel = {}
        n = 700
        for i in range(25):
            df = _mk(n, seed=95000 + i)
            fac = reg.compute(df, names)
            rng = np.random.default_rng(96000 + i)
            # 只在较早起点(2024)有信号, 较晚起点(2025)为纯噪声
            mask = fac.index >= pd.Timestamp("20250101")
            sig = np.where(mask, 0.0, 0.12)
            fac["fwd_ret"] = fac["mom_20"] * sig + rng.normal(0, 0.005, n)
            panel[f"S{i}"] = fac
        rows = ic.scan_and_summarize(
            panel,
            horizons=(5,),
            starts=("20240101", "20250101"),
            min_combos=2,
            recompute_fwd=False,
            robust_ratio=1.0,
        )
        mom = next(r for r in rows if r["factor"] == "mom_20")
        assert mom["n_significant"] < 2, "应只有部分组合显著"
        assert not mom["robust"], "要求全数组合显著时, 仅部分显著的不应判稳健"
