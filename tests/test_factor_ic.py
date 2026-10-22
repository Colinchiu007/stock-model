"""IC 分析扩样本(研究方案 Phase A: 把结论可信度提上去)

为什么先做这个而不是直接做 UI
-----------------------------
上一轮 IC 分析只用了 3 只标的, 截面 Spearman 相关在 n=3 时波动极大,
**统计意义有限**(脚本里已标注)。照这个结论去选因子 = 拿噪声当信号。
研究方案 §5 的原话: "结论用于排序筛选, 不用于直接启用策略" ——
所以本轮先把样本扩到 20+ 只, 让 IC 具备统计意义, 再花工程量做 UI。

契约:
  1. 样本量门槛: 有效标的 < MIN_SYMBOLS 时**拒绝出结论**(不静默降级)
  2. IC 显著性: 报告 t 统计量与 p 值, 而不只是 IC 均值
     —— n 大时很小的 IC 也可能显著; 不看 p 值会误判
  3. 多重检验: 同时测 13 个因子, 必须做 Bonferroni/FDR 校正
     —— 否则"最显著的那个"很可能只是运气
  4. 因子面板构建与单只计算分离(便于测试与复用)
  5. 未来收益严格用 shift(-N): 不得引入未来函数
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
    """experiments/ 不是包, 用 spec 加载(与既有 experiment 测试同款)"""
    path = REPO_ROOT / "experiments" / "factor_ic.py"
    spec = importlib.util.spec_from_file_location("_factor_ic", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def ic():
    return load_ic()


def _mk(n: int = 300, seed: int = 0) -> pd.DataFrame:
    import numpy as np

    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2024-01-01", periods=n)
    ret = rng.normal(0.0003, 0.015, n)
    close = pd.Series(10.0 * (1 + ret).cumprod(), index=idx)
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


def _panel(
    n_symbols: int = 25, n: int = 300, seed0: int = 0, fwd_days: int = 5
) -> dict[str, pd.DataFrame]:
    """构造"已算好因子 + 未来收益"的面板(ic_series 的输入形态)"""
    reg = default_registry()
    names = reg.list()
    out: dict[str, pd.DataFrame] = {}
    for i in range(n_symbols):
        df = _mk(n, seed=seed0 + i)
        fac = reg.compute(df, names)
        # 未来收益基于原始 close(fac 里没有 close)
        fac["fwd_ret"] = df["close"].shift(-fwd_days) / df["close"] - 1.0
        out[f"S{i}"] = fac
    return out


class TestICStatistics:
    def test_sample_size_gate_rejects_small_panel(self, ic):
        """样本不足时必须拒绝出结论(不能拿 n=3 当结论)"""
        panel = _panel(n_symbols=2, n=200)  # 只有 2 只
        with pytest.raises(ValueError, match="样本不足"):
            ic.analyze(panel, min_symbols=10)

    def test_ic_report_contains_p_value(self, ic):
        """结果必须含 p 值(不看显著性会误判噪声)"""
        panel = _panel(n_symbols=25, n=300)
        rows = ic.analyze(panel, min_symbols=10)
        assert rows, "应产出结果"
        for r in rows:
            assert "p_value" in r, f"缺 p_value: {r}"

    def test_ic_report_contains_t_stat(self, ic):
        panel = _panel(n_symbols=25, n=300)
        rows = ic.analyze(panel, min_symbols=10)
        for r in rows:
            assert "t_stat" in r

    def test_multiple_testing_correction_applied(self, ic):
        """13 个因子同时检验, 必须做多重检验校正"""
        panel = _panel(n_symbols=25, n=300)
        rows = ic.analyze(panel, min_symbols=10)
        checked = 0
        for r in rows:
            assert "p_corrected" in r, "缺校正后 p 值"
            if np.isnan(r["p_value"]):
                # 随机数据下个别因子可能无有效截面, 只要求同为 NaN
                assert np.isnan(r["p_corrected"])
                continue
            # 校正后的 p 不得小于原始 p(只会更保守)
            assert r["p_corrected"] >= r["p_value"] - 1e-12
            checked += 1
        assert checked > 0, "应有可比较的因子"

    def test_correction_is_actually_applied(self, ic):
        """反向验证: 校正必须**真的放大** p 值, 而不只是字段存在

        变异验证发现「去掉校正」时前面的测试仍绿 —— 因为随机数据 p 都大,
        校正与否都不显著。故补这条: 用强信号让 p 很小, 校正后必须明显变大。
        """
        reg = default_registry()
        names = reg.list()
        panel: dict[str, pd.DataFrame] = {}
        for i in range(25):
            df = _mk(300, seed=5000 + i)
            fac = reg.compute(df, names)
            # 中等强度信号: 让 p 落在 0.001~0.05 之间, 便于观察校正放大效果
            # (完全线性 -> IC 恒 1、std=0 -> t 为 NaN; 过弱 -> p 不够小)
            rng = np.random.default_rng(9000 + i)
            fac["fwd_ret"] = fac["mom_20"] * 0.05 + rng.normal(0, 0.01, len(fac))
            panel[f"S{i}"] = fac

        rows = ic.analyze(panel, min_symbols=10)
        strong = next(r for r in rows if r["factor"] == "mom_20")
        assert not np.isnan(strong["p_value"]), "强信号下 p 不应为 NaN"
        assert 0.0 < strong["p_value"] < 0.05, (
            f"强信号下原始 p 应在 (0, 0.05), 实际 {strong['p_value']}"
        )
        assert strong["p_corrected"] > strong["p_value"], (
            f"校正必须放大 p 值: 原始 {strong['p_value']:.6f}, 校正后 {strong['p_corrected']:.6f}"
        )

    def test_random_data_yields_no_significant_factor(self, ic):
        """随机游走数据: 不应有因子通过校正后显著性检验

        这是**反向验证**: 若随机数据都能找出"显著因子", 说明方法有问题
        (过拟合 / 未校正 / 未来函数)。
        """
        panel = _panel(n_symbols=30, n=400, seed0=1000)
        rows = ic.analyze(panel, min_symbols=10)
        significant = [r for r in rows if r["p_corrected"] < 0.05]
        assert not significant, (
            f"随机数据不该有显著因子, 实际: "
            f"{[(r['factor'], r['p_corrected']) for r in significant]}"
        )


class TestPanelBuilding:
    def test_fwd_ret_uses_negative_shift(self, ic):
        """未来收益必须 shift(-N): 用 shift(+N) 就是未来函数"""
        df = _mk(100)
        out = ic.add_forward_return(df, df["close"], days=5)
        expected = df["close"].shift(-5) / df["close"] - 1.0
        pd.testing.assert_series_equal(out["fwd_ret"], expected, check_names=False)

    def test_fwd_ret_last_n_are_nan(self, ic):
        """最后 N 根没有未来数据, 必须是 NaN(而非 0 或回填)"""
        df = _mk(100)
        out = ic.add_forward_return(df, df["close"], days=5)
        assert out["fwd_ret"].iloc[-5:].isna().all(), "末 N 根应为 NaN"

    def test_factors_computed_for_all_symbols(self):
        """面板中每只都要算全因子(不能漏)"""
        reg = default_registry()
        names = reg.list()
        for i in range(5):
            df = _mk(200, seed=i)
            fac = reg.compute(df, names)
            assert list(fac.columns) == names
            assert len(fac) == len(df)
