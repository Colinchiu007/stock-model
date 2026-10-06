"""指标降级与选股 API 回归保护测试 (QM-5 ④)

背景
----
项目长期存在一个隐蔽的 P0: ``analysis/technical.py`` 的 MACD/RSI/BOLL/ATR/OBV
全部依赖 ``pandas-ta``，而 pandas-ta 仅支持 Python >= 3.12，项目声明支持 >= 3.10。

在 3.10/3.11 上缺失依赖时原实现是：
    rsi_result = ta.rsi(...) if HAS_PANDAS_TA else None
    if rsi_result is not None:
        df[f"rsi{period}"] = rsi_result
    return df          # ← 静默返回，指标列根本不产生

后果链条（实测）：
    指标列缺失 → 信号生成器读不到 MACD/RSI/BOLL → 对所有股票返回 hold
    → 实测 5 只股票全部 hold，策略实质失效，且无任何报错。

本文件锁定两件事：
  1. 无论 pandas-ta 是否可用，技术指标都必须产出
  2. 选股/分析/回测三个 API 的返回结构稳定
"""

import numpy as np
import pandas as pd
import pytest

from stock_model.analysis import indicators as ind


def _make_df(rows: int = 200, seed: int = 42) -> pd.DataFrame:
    """构造可复现的行情数据（随机游走，带真实波动）"""
    rng = np.random.default_rng(seed)
    close = 10 + np.cumsum(rng.normal(0.02, 0.18, rows))
    close = np.abs(close) + 5.0
    idx = pd.date_range("2023-01-01", periods=rows, freq="B")
    return pd.DataFrame(
        {
            "open": close * 0.998,
            "high": close * 1.012,
            "low": close * 0.988,
            "close": close,
            "volume": rng.integers(1_000_000, 5_000_000, rows).astype(float),
        },
        index=idx,
    )


# ========================================================================
# 核心回归：指标必须无条件产出
# ========================================================================


class TestIndicatorsAlwaysProduced:
    """无论 pandas-ta 是否安装，指标列都必须存在"""

    def test_analyze_all_produces_all_columns(self):
        df = ind.analyze_all(_make_df())
        expected = [
            "MACD_12_26_9",
            "MACDs_12_26_9",
            "MACDh_12_26_9",
            "RSI_14",
            "BBM_20_2.0",
            "BBU_20_2.0",
            "BBL_20_2.0",
            "ATR_14",
            "KDJ_K",
            "KDJ_D",
            "KDJ_J",
            "OBV",
        ]
        missing = [c for c in expected if c not in df.columns]
        assert not missing, f"指标列缺失: {missing}"

    def test_technical_analysis_analyze_all_has_indicator_columns(self):
        """技术分析器聚合后必须含信号生成器依赖的列

        列名契约由 signals.py 的 _find_column 决定：
        MACD_ / MACDs_ / RSI / BBL_ / BBU_
        """
        from stock_model.analysis.technical import TechnicalAnalysis

        df = TechnicalAnalysis().analyze_all(_make_df())

        for prefix in ("MACD_", "MACDs_", "RSI", "BBL_", "BBU_", "ATR_", "obv", "kdj_k"):
            assert any(c.startswith(prefix) for c in df.columns), f"缺少以 {prefix} 开头的指标列"

    def test_signal_generator_can_read_indicator_columns(self):
        """信号生成器必须能从 analyze_all 的产物中读到指标

        这是整条链的终点校验：指标存在但列名对不上，信号依然全为 0。
        """
        from stock_model.analysis.signals import SignalGenerator
        from stock_model.analysis.technical import TechnicalAnalysis

        sg = SignalGenerator()
        df = TechnicalAnalysis().analyze_all(_make_df(300))

        assert sg._find_column(df, "MACD_") is not None
        assert sg._find_column(df, "MACDs_") is not None
        assert sg._find_column(df, "RSI") is not None
        assert sg._find_column(df, "BBL_") is not None
        assert sg._find_column(df, "BBU_") is not None


# ========================================================================
# 数值正确性：公式必须对
# ========================================================================


class TestIndicatorNumerics:
    """数值正确性 —— 公式错了比不实现更危险"""

    def test_macd_matches_manual_ema(self):
        """MACD DIF 应精确等于 EMA(fast) - EMA(slow)"""
        df = _make_df(100, seed=7)
        result = ind.macd(df, 12, 26, 9)

        closes = df["close"].to_numpy()

        def ema(values: np.ndarray, span: int) -> float:
            alpha = 2.0 / (span + 1)
            out = values[0]
            for v in values[1:]:
                out = alpha * v + (1 - alpha) * out
            return out

        expected = ema(closes, 12) - ema(closes, 26)
        actual = float(result["MACD_12_26_9"].iloc[-1])
        assert actual == pytest.approx(expected, abs=1e-9)

    def test_rsi_bounds(self):
        """RSI 必须落在 [0, 100]"""
        result = ind.rsi(_make_df(200), 14)
        vals = result["RSI_14"].dropna()
        assert vals.between(0, 100).all()

    def test_rsi_flat_is_neutral(self):
        """横盘时 RSI 应为 50（无涨跌方向）"""
        flat = pd.DataFrame({"close": [10.0] * 40})
        result = ind.rsi(flat, 14)
        assert float(result["RSI_14"].iloc[-1]) == pytest.approx(50.0, abs=1e-6)

    def test_rsi_monotonic_up_is_100(self):
        """单调上涨时 RSI 应接近 100"""
        up = pd.DataFrame({"close": np.arange(1.0, 51.0)})
        result = ind.rsi(up, 14)
        assert float(result["RSI_14"].iloc[-1]) == pytest.approx(100.0, abs=1e-6)

    def test_boll_ordering(self):
        """布林带必须满足 下轨 ≤ 中轨 ≤ 上轨"""
        result = ind.bollinger(_make_df(100), 20, 2.0)
        tail = result.tail(50)
        assert (tail["BBL_20_2.0"] <= tail["BBM_20_2.0"]).all()
        assert (tail["BBM_20_2.0"] <= tail["BBU_20_2.0"]).all()

    def test_atr_non_negative(self):
        result = ind.atr(_make_df(100), 14)
        assert (result["ATR_14"].dropna() >= 0).all()

    def test_no_inf_or_nan_after_warmup(self):
        """预热期后不应出现 inf；整体不应产生 inf"""
        result = ind.analyze_all(_make_df(200))
        numeric = result.select_dtypes(include=[np.number])
        assert not np.isinf(numeric.to_numpy()).any()

    def test_input_dataframe_not_mutated(self):
        """函数不得修改传入的 DataFrame（技术分析器依赖此行为）"""
        df = _make_df(60)
        before = df.copy()
        ind.analyze_all(df)
        pd.testing.assert_frame_equal(df, before)

    def test_flat_data_has_no_division_error(self):
        """最高价=最低价时 KDJ/BOLL 不应除零"""
        n = 40
        flat = pd.DataFrame(
            {
                "open": [10.0] * n,
                "high": [10.0] * n,
                "low": [10.0] * n,
                "close": [10.0] * n,
                "volume": [1000.0] * n,
            }
        )
        result = ind.analyze_all(flat)
        numeric = result.select_dtypes(include=[np.number])
        assert not np.isinf(numeric.to_numpy()).any()
        assert not np.isnan(result["KDJ_K"].iloc[-1])

    def test_missing_columns_raises_clearly(self):
        """缺列时给出明确错误，而非静默产出垃圾"""
        with pytest.raises(ValueError, match="缺少必需列"):
            ind.analyze_all(pd.DataFrame({"close": [1.0, 2.0]}))


# ========================================================================
# 选股 API
# ========================================================================


@pytest.fixture(scope="module")
def client():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from stock_model.web.app import create_app

    return TestClient(create_app())


class TestScreenerAPI:
    """选股榜 API 的返回结构契约"""

    def test_screener_returns_expected_shape(self, client):
        """结构契约: results/summary 必须存在，且字段齐全

        用 mock 数据源避免真实网络请求。
        """
        from unittest.mock import MagicMock, patch

        import stock_model.web.screener as screener_mod

        fake_df = _make_df(120)
        fake_result = MagicMock()
        fake_result.action.value = "buy"
        fake_result.confidence = 0.8
        fake_result.target_price = 12.5
        fake_result.stop_loss = 9.0
        fake_result.position_pct = 30.0
        fake_result.reason = "测试信号"
        fake_result.metadata = {"trend": "up"}

        fake_signal = MagicMock()
        fake_signal.signal_type.value = "buy"
        fake_signal.strength.value = "strong"
        fake_signal.reason = "MA金叉"
        fake_signal.price = 10.0
        fake_signal.timestamp = None

        fake_ta = MagicMock()
        fake_ta.analyze_all.return_value = fake_df
        fake_sg = MagicMock()
        fake_sg.generate_all.return_value = [fake_signal]

        with (
            patch.object(screener_mod, "_analyze_one") as mocked,
        ):
            mocked.return_value = {
                "symbol": "000001",
                "action": "buy",
                "confidence": 0.8,
                "target_price": 12.5,
                "stop_loss": 9.0,
                "position_pct": 30.0,
                "reason": "测试信号",
                "signals": [
                    {
                        "type": "buy",
                        "strength": "strong",
                        "reason": "MA金叉",
                        "price": 10.0,
                        "timestamp": None,
                    }
                ],
                "score": 5.0,
                "close": 10.5,
                "error": None,
                "analyzed_at": "2024-01-01T00:00:00",
            }
            resp = client.get("/api/screener?symbols=000001")

        assert resp.status_code == 200
        data = resp.json()
        assert "results" in data
        assert "summary" in data
        assert data["summary"]["scanned"] == 1

        row = data["results"][0]
        for field in (
            "symbol",
            "action",
            "confidence",
            "score",
            "target_price",
            "stop_loss",
            "position_pct",
            "signals",
            "close",
            "error",
        ):
            assert field in row, f"选股结果缺少字段 {field}"

    def test_screener_handles_empty_symbols(self, client):
        resp = client.get("/api/screener?symbols=")
        assert resp.status_code == 200
        assert resp.json()["results"] == []

    def test_screener_summary_counts_actions(self, client):
        from unittest.mock import patch

        import stock_model.web.screener as screener_mod

        def fake_analyze(symbol, *args, **kwargs):
            action = {"000001": "buy", "000002": "sell", "600036": "hold"}[symbol]
            return {
                "symbol": symbol,
                "action": action,
                "confidence": 0.5,
                "target_price": None,
                "stop_loss": None,
                "position_pct": 0.0,
                "reason": "",
                "signals": [],
                "score": 1.0,
                "close": 10.0,
                "error": None,
                "analyzed_at": "2024-01-01T00:00:00",
            }

        with patch.object(screener_mod, "_analyze_one", side_effect=fake_analyze):
            resp = client.get("/api/screener?symbols=000001,000002,600036")

        counts = resp.json()["summary"]["action_counts"]
        assert counts.get("buy") == 1
        assert counts.get("sell") == 1
        assert counts.get("hold") == 1


# ========================================================================
# 前端资源
# ========================================================================


class TestFrontendAssets:
    """前端资源必须真实存在且可访问"""

    def test_index_page_served(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "选股榜" in resp.text

    def test_static_css_served(self, client):
        resp = client.get("/static/dashboard.css")
        assert resp.status_code == 200
        assert "--buy" in resp.text

    def test_static_js_served(self, client):
        resp = client.get("/static/dashboard.js")
        assert resp.status_code == 200
        assert "screener" in resp.text

    def test_index_is_not_inlined_in_python(self):
        """仪表盘页面不应再内嵌在 app.py 里

        历史问题: 287 行 HTML 字符串塞在 Python 源文件中, 无法测试与维护。
        允许存在极简的错误兜底页(不含 <style>/<script>), 但不允许再内嵌
        完整仪表盘。
        """
        from pathlib import Path

        import stock_model.web as web_pkg

        app_py = Path(web_pkg.__file__).parent / "app.py"
        src = app_py.read_text(encoding="utf-8")

        # 完整仪表盘必然包含这些标记, 错误兜底页不会
        assert "<style>" not in src, "app.py 不应内嵌完整 HTML 页面(检测到 <style>)"
        assert '<div class="grid">' not in src, (
            "app.py 仍内嵌仪表盘结构(检测到 grid 布局标记), 前端应已拆分到 static/"
        )
        assert len(src) < 40000, f"app.py 体积 {len(src)} 字节, 疑似内嵌了大段页面代码"

    def test_static_files_exist_on_disk(self):
        from pathlib import Path

        import stock_model.web as web_pkg

        static = Path(web_pkg.__file__).parent / "static"
        for name in ("index.html", "dashboard.css", "dashboard.js"):
            assert (static / name).is_file(), f"缺少前端资源 static/{name}"
