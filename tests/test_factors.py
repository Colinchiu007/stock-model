"""因子注册表 + 内置因子的契约测试(Phase A)

设计动机(2026-10-22 研究方案):
  现有"因子"其实是硬编码在 signals.py 的 4 条技术规则 —— 想试
  "RSI+MACD 不要布林带"必须改代码。后续 ML / SHAP 归因 / 参数优化
  全部依赖一个可组合、可按需计算的因子层, 故先做这一层。

契约:
  1. 因子是**纯函数**: 同输入必同输出(否则归因/回测不可信)
  2. 按需计算: 未勾选的因子不消耗 CPU
  3. 注册/列举/计算三类 API 齐备
  4. 输出长度与输入一致(便于对齐索引做截面分析)
  5. 未知因子名必须显式报错, 不能静默跳过
"""

from __future__ import annotations

import pandas as pd
import pytest

from stock_model.analysis.factors import FactorRegistry, default_registry


def _mk(n: int = 300) -> pd.DataFrame:
    """合成 OHLCV: 带趋势+震荡, 便于因子计算"""
    idx = pd.bdate_range("2024-01-01", periods=n)
    close = pd.Series(
        [10.0 + i * 0.02 + (3.0 if i % 20 < 10 else -3.0) for i in range(n)], index=idx
    )
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


class TestRegistryBasics:
    def test_register_and_list(self):
        reg = FactorRegistry()

        @reg.register("dummy")
        def _f(df):
            return df["close"] * 2

        assert "dummy" in reg.list()

    def test_compute_selected_only(self, monkeypatch):
        """未勾选的因子不得被调用(按需计算)"""
        reg = FactorRegistry()
        calls: list[str] = []

        @reg.register("a")
        def _a(df):
            calls.append("a")
            return df["close"]

        @reg.register("b")
        def _b(df):
            calls.append("b")
            return df["close"] * 2

        reg.compute(_mk(), ["a"])
        assert calls == ["a"], f"未勾选的 b 不该被调用, 实际 {calls}"

    def test_compute_returns_only_selected(self):
        reg = default_registry()
        df = _mk()
        out = reg.compute(df, ["mom_20", "rsi_14"])
        assert set(out.columns) == {"mom_20", "rsi_14"}

    def test_unknown_factor_raises(self):
        """未知因子名必须显式报错, 且报错里带出可用因子(便于排查)"""
        reg = default_registry()
        with pytest.raises(KeyError, match="not_a_factor") as ei:
            reg.compute(_mk(), ["not_a_factor"])
        # 报错信息必须列出已注册因子, 否则用户只知道"错了"不知道"有哪些"
        assert "mom_20" in str(ei.value), f"报错应列出可用因子: {ei.value}"

    def test_unknown_factor_raises_before_any_work(self):
        """未知名字必须**在算任何因子之前**就报错(快速失败)"""
        reg = default_registry()
        calls: list[str] = []

        @reg.register("known")
        def _k(df):
            calls.append("known")
            return df["close"]

        with pytest.raises(KeyError):
            reg.compute(_mk(), ["known", "bogus"])
        assert calls == [], f"应先校验名字再计算, 实际已调用 {calls}"

    def test_output_length_matches_input(self):
        """因子输出长度必须等于输入(否则无法对齐截面)"""
        reg = default_registry()
        df = _mk(200)
        out = reg.compute(df, reg.list())
        assert len(out) == len(df)
        for col in out.columns:
            assert len(out[col]) == len(df), f"{col} 长度不符"

    def test_purity_same_input_same_output(self):
        """纯函数: 同输入两次计算结果一致"""
        reg = default_registry()
        df = _mk()
        a = reg.compute(df.copy(), ["mom_20", "rsi_14"])
        b = reg.compute(df.copy(), ["mom_20", "rsi_14"])
        pd.testing.assert_frame_equal(a, b)

    def test_does_not_mutate_input(self):
        """不得修改传入的 DataFrame"""
        reg = default_registry()
        df = _mk()
        before = df.copy()
        reg.compute(df, ["mom_20", "rsi_14"])
        pd.testing.assert_frame_equal(df, before)


class TestBuiltinFactors:
    def test_momentum_sign(self):
        """上涨序列的动量因子应为正"""
        reg = default_registry()
        idx = pd.bdate_range("2024-01-01", periods=100)
        up = pd.Series([10.0 + i * 0.1 for i in range(100)], index=idx)
        df = pd.DataFrame(
            {"open": up, "high": up * 1.01, "low": up * 0.99, "close": up, "volume": [1e6] * 100},
            index=idx,
        )
        out = reg.compute(df, ["mom_20"])
        assert out["mom_20"].iloc[-1] > 0, "上涨时动量应为正"

    def test_reversal_is_negated_momentum(self):
        """反转 = 负动量"""
        reg = default_registry()
        df = _mk()
        out = reg.compute(df, ["mom_20", "rev_20"])
        pd.testing.assert_series_equal(out["rev_20"], -out["mom_20"], check_names=False)

    def test_rsi_bounded(self):
        """RSI 必须落在 [0, 100]"""
        reg = default_registry()
        out = reg.compute(_mk(), ["rsi_14"])
        s = out["rsi_14"].dropna()
        assert s.between(0, 100).all(), f"RSI 越界: min={s.min()}, max={s.max()}"

    def test_bb_position_bounded(self):
        """布林带分位应在 [0,1] 附近(允许超出, 因价格可越界)"""
        reg = default_registry()
        out = reg.compute(_mk(), ["bb_position"])
        s = out["bb_position"].dropna()
        assert s.notna().all()
        # 允许略超 [0,1], 但不应出现极端离谱值
        assert s.abs().max() < 10, f"bb_position 异常: {s.abs().max()}"

    def test_volume_ratio_positive(self):
        reg = default_registry()
        out = reg.compute(_mk(), ["volume_ratio"])
        s = out["volume_ratio"].dropna()
        assert (s > 0).all(), "量比应为正"

    def test_default_registry_has_expected_factors(self):
        """第一批至少覆盖: 动量 / 反转 / 波动 / 量价 / 技术"""
        names = default_registry().list()
        for required in ["mom_20", "rev_20", "vol_20", "volume_ratio", "rsi_14"]:
            assert required in names, f"缺因子 {required}"
