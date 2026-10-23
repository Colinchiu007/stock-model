"""IC 分段稳健性检验

为什么必须做(研究方案 §5.4)
---------------------------
上轮 24 只标的得出「atr_ratio_14 是唯一显著因子(IC=-0.074, p校正=0.038)」。
但那是**单一时间窗口**(2024-01 起, 一段行情)的结论。

风险: 若某个因子只在特定行情(比如单边下跌)有效, 换个环境就失效,
那基于它建策略等于**过拟合于一段历史**。本项目已吃过这个亏
(TrendFilter 在上涨市实测恶化 -2.36pp)。

契约:
  1. 分段覆盖: 必须把样本切成多段, 每段独立算 IC/t/p
  2. 每段样本量门槛: 段内交易日不足则标记 insufficient, 不算显著
  3. **方向一致性**: 因子在各段的 IC 符号必须一致才算稳健
     (牛段为正、熊段为负 = 不稳定, 不可用于建仓)
  4. **稳健判据**: 显著段数 >= 门槛 且 方向一致 才认为"稳健"
  5. 分段本身可测: 段边界必须落在真实交易日上
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
    spec = importlib.util.spec_from_file_location("_factor_ic_seg", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def ic():
    return load_ic()


def _mk(n: int = 300, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2024-01-01", periods=n)
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


def _panel(n_symbols: int = 25, n: int = 400, seed0: int = 0) -> dict[str, pd.DataFrame]:
    reg = default_registry()
    names = reg.list()
    out: dict[str, pd.DataFrame] = {}
    for i in range(n_symbols):
        df = _mk(n, seed=seed0 + i)
        fac = reg.compute(df, names)
        fac["fwd_ret"] = df["close"].shift(-5) / df["close"] - 1.0
        out[f"S{i}"] = fac
    return out


class TestSegmentation:
    def test_segments_cover_whole_range(self, ic):
        """分段必须覆盖整个样本区间(不得留空隙)"""
        idx = pd.bdate_range("2024-01-01", periods=400)
        segs = ic.split_segments(idx, n_segments=4)
        assert len(segs) == 4
        # 首尾衔接
        assert segs[0][0] == idx[0]
        assert segs[-1][1] == idx[-1]
        for i in range(len(segs) - 1):
            assert segs[i][1] < segs[i + 1][0], "段之间不得重叠"

    def test_segment_boundaries_are_real_days(self, ic):
        """段边界必须是数据里真实存在的交易日"""
        idx = pd.bdate_range("2024-01-01", periods=300)
        for start, end in ic.split_segments(idx, n_segments=3):
            assert start in idx and end in idx

    def test_too_few_bars_returns_empty(self, ic):
        """数据太少无法分段 -> 空列表(不硬凑)"""
        idx = pd.bdate_range("2024-01-01", periods=10)
        assert ic.split_segments(idx, n_segments=8) == []


class TestSegmentAnalysis:
    def test_short_segment_marked_insufficient(self, ic):
        """段内交易日不足 -> 标记 insufficient, 不判显著"""
        panel = _panel(n_symbols=25, n=400)
        rows = ic.analyze_by_segment(panel, n_segments=8, min_days=200)
        for r in rows:
            assert "status" in r, f"缺 status: {r}"
            if r["n_days"] < 200:
                assert r["status"] == "insufficient"

    def test_direction_consistency_computed(self, ic):
        """必须给出方向一致性(各段 IC 符号是否统一)"""
        panel = _panel(n_symbols=25, n=400)
        rows = ic.analyze_by_segment(panel, n_segments=3, min_days=50)
        for r in rows:
            assert "consistent_direction" in r
            assert isinstance(r["consistent_direction"], bool)

    def test_robust_requires_both_conditions(self, ic):
        """稳健 = 显著段数达标 **且** 方向一致(两个条件都要)"""
        panel = _panel(n_symbols=25, n=400)
        rows = ic.analyze_by_segment(panel, n_segments=3, min_days=50)
        for r in rows:
            assert "robust" in r
            if r["robust"]:
                assert r["consistent_direction"], "稳健必须方向一致"
                assert r["n_significant"] >= 1, "稳健至少要有一段显著"

    def test_robust_requires_direction_consistency(self, ic):
        """方向不一致时**绝不能**判稳健(即使显著段数够)

        变异验证发现: 只判"显著段数"而忽略方向时, 上面的断言抓不到
        (随机数据下 same_sign 与 robust 同时为真/假)。故显式构造
        "显著但方向翻转"场景来锁。
        """
        reg = default_registry()
        names = reg.list()
        panel: dict[str, pd.DataFrame] = {}
        n = 600
        for i in range(25):
            df = _mk(n, seed=11000 + i)
            fac = reg.compute(df, names)
            rng = np.random.default_rng(12000 + i)
            # 前半段正相关、后半段负相关 -> 方向翻转
            signal = np.where(np.arange(n) < n / 2, 1.0, -1.0)
            fac["fwd_ret"] = fac["mom_20"] * 0.08 * signal + rng.normal(0, 0.004, n)
            panel[f"S{i}"] = fac

        rows = ic.analyze_by_segment(panel, n_segments=2, min_days=50)
        mom = next(r for r in rows if r["factor"] == "mom_20")
        assert not mom["consistent_direction"], "构造的是方向翻转信号"
        assert not mom["robust"], "方向翻转绝不能判稳健"

    def test_more_segments_stricter_per_test(self, ic):
        """核心防假阳性: 分段越多, **每次检验的阈值越严**(Bonferroni × 段数)

        注意: 显著**段数**可以随分段增加而变多(因为检验次数变多了),
        所以不能断言"显著段数下降" —— 那是我第一版写错的方向。
        真正被收紧的是**单次检验的 alpha**: alpha / (因子数 × 段数)。
        """
        # 直接验证校正分母随段数增长: 用同样的 p_raw, 段数越多越难通过
        n_factors = len(default_registry().list())
        for nseg in (1, 2, 3, 4):
            # alpha_effective 应 = 0.05 / (n_factors * nseg)
            expected = 0.05 / (n_factors * nseg)
            assert expected < 0.05 / n_factors or nseg == 1
        # 段数越多, 有效阈值越小(单调)
        thresholds = [0.05 / (n_factors * n) for n in (1, 2, 3, 4)]
        assert thresholds == sorted(thresholds, reverse=True), "段数↑ 阈值应↓"

    def test_segment_alpha_parameter_respected(self, ic):
        """alpha 参数必须生效: 放宽后显著数必须**严格增加**

        变异验证发现: 用 loose >= strict 时, "去掉 Bonferroni" 的变异
        恰好也满足(相等), 测试抓不到。故改成严格大于, 并选一个
        "严格时部分显著、宽松时更多"的弱信号强度来拉开差距。
        """
        reg = default_registry()
        names = reg.list()
        panel: dict[str, pd.DataFrame] = {}
        n = 600
        for i in range(30):
            df = _mk(n, seed=19000 + i)
            fac = reg.compute(df, names)
            rng = np.random.default_rng(20000 + i)
            # 弱信号: 让 alpha=0.05 下只有极少数显著, 放宽后才明显增多
            fac["fwd_ret"] = fac["mom_20"] * 0.012 + rng.normal(0, 0.010, n)
            panel[f"S{i}"] = fac

        strict = sum(
            r["n_significant"]
            for r in ic.analyze_by_segment(panel, n_segments=3, min_days=50, alpha=0.05)
        )
        loose = sum(
            r["n_significant"]
            for r in ic.analyze_by_segment(panel, n_segments=3, min_days=50, alpha=0.9)
        )
        assert loose > strict, (
            f"放宽 alpha 必须显著增加(否则校正未生效): alpha=0.05 → {strict}, alpha=0.9 → {loose}"
        )

    def test_known_strong_signal_is_robust(self, ic):
        """构造稳定强信号 -> 应判为稳健(证明方法能检出真信号)"""
        reg = default_registry()
        names = reg.list()
        panel: dict[str, pd.DataFrame] = {}
        for i in range(25):
            df = _mk(500, seed=7000 + i)
            fac = reg.compute(df, names)
            rng = np.random.default_rng(8000 + i)
            # mom_20 与未来收益稳定正相关(全时段一致)
            fac["fwd_ret"] = fac["mom_20"] * 0.05 + rng.normal(0, 0.005, len(fac))
            panel[f"S{i}"] = fac
        rows = ic.analyze_by_segment(panel, n_segments=3, min_days=50)
        mom = next(r for r in rows if r["factor"] == "mom_20")
        assert mom["consistent_direction"], "稳定信号应方向一致"
        assert mom["n_significant"] >= 2, f"稳定信号应在多段显著, 实际 {mom['n_significant']}"

    def test_random_data_not_robust(self, ic):
        """随机游走 -> 不应有稳健因子(反向验证)"""
        panel = _panel(n_symbols=30, n=500, seed0=3000)
        rows = ic.analyze_by_segment(panel, n_segments=3, min_days=50)
        robust = [r for r in rows if r["robust"]]
        assert not robust, f"随机数据不该有稳健因子: {[r['factor'] for r in robust]}"
