"""fetcher 双向降级 —— baostock 作主源失败时也要能降级到 akshare

缺陷(2026-10-13 凌晨实测):
  账户落盘 data_source=baostock, baostock 整体抖动时**无备选源** ——
  fetcher 的降级链原来只有 akshare→baostock 一个方向。即使 akshare 可用
  也切不过去, 整个模拟盘瘫痪(兜底补跑 status=error, E2E 复活链红)。

修复契约:
1. baostock 作主源 → 备选 = akshare(反向降级)
2. akshare 作主源 → 备选 = baostock(现有方向不变)
3. 主源连接失败 → 尝试备选; 备选成功 → 用备选数据
4. 两个都失败 → 抛真实根因(双源原因都在消息里)

设计: 源构造被 patch 成假源(**不碰网络**) —— 测的是降级逻辑本身,
不是数据源可用性(那是另一回事, 由 test_baostock_relogin 系列覆盖)。
"""

from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from stock_model.data.fetcher import StockDataFetcher


class FakeSource:
    """假数据源: name + get_daily(可配成败/数据)"""

    def __init__(self, name: str, fail: bool = False, exc: Exception | None = None):
        self.name = name
        self._fail = fail
        self._exc = exc or RuntimeError(f"{name}连接失败")
        self.calls: list[str] = []

    def get_daily(self, symbol, *a, **k):
        self.calls.append(symbol)
        if self._fail:
            raise self._exc
        idx = pd.date_range("2026-01-01", periods=80, freq="D")
        return pd.DataFrame(
            {
                "open": [10.0] * 80,
                "high": [10.1] * 80,
                "low": [9.9] * 80,
                "close": [10.0] * 80,
                "volume": [1_000_000] * 80,
            },
            index=idx,
        )


def _make_fetcher(primary: str, fallback: FakeSource) -> StockDataFetcher:
    """构造 fetcher, 但把两个源都换成假源(不碰网络)"""
    fake_primary = FakeSource(primary)
    with patch.object(
        StockDataFetcher,
        "_create_source",
        side_effect=lambda name: fake_primary if name == primary else fallback,
    ):
        f = StockDataFetcher(source=primary)
    f._source_instance = fake_primary
    f._fallback_instance = fallback
    f._cache_enabled = False  # 隔离: 真实磁盘缓存会让源根本不被调用(实测踩过)
    return f


class TestBidirectionalFallback:
    def test_baostock_primary_has_akshare_fallback(self):
        """baostock 作主源 → 备选必须是 akshare(反向降级)"""
        fb = FakeSource("akshare")
        f = _make_fetcher("baostock", fb)
        assert f._fallback_instance is not None
        assert f._fallback_instance.name == "akshare"

    def test_akshare_primary_still_has_baostock_fallback(self):
        """现有方向不变: akshare 主 → baostock 备"""
        fb = FakeSource("baostock")
        f = _make_fetcher("akshare", fb)
        assert f._fallback_instance is not None
        assert f._fallback_instance.name == "baostock"

    def test_baostock_down_falls_back_to_akshare(self):
        """baostock 连接失败 → 自动切 akshare 并拿到数据"""
        fb = FakeSource("akshare")
        f = _make_fetcher("baostock", fb)
        f._source_instance._fail = True
        f._source_instance._exc = RuntimeError("baostock登录失败: 10002007 网络接收错误")

        df = f.get_daily("000001")

        assert not df.empty, "降级后应拿到数据"
        assert f._source_instance.calls == ["000001"], "主源应被尝试过"
        assert fb.calls == ["000001"], "备选源应被尝试"

    def test_both_down_raises_real_reason(self):
        """两个源都失败 → 抛真实根因(双源原因都在消息里)"""
        fb = FakeSource("akshare", fail=True, exc=RuntimeError("akshare: RemoteDisconnected"))
        f = _make_fetcher("baostock", fb)
        f._source_instance._fail = True
        f._source_instance._exc = RuntimeError("baostock登录失败: 10002007")

        with pytest.raises(RuntimeError) as e:
            f.get_daily("000001")
        msg = str(e.value)
        assert "baostock" in msg and "akshare" in msg, f"应含双源原因: {msg}"

    def test_primary_ok_no_fallback_call(self):
        """主源正常 → 不碰备选源(不浪费一次网络往返)"""
        fb = FakeSource("akshare")
        f = _make_fetcher("baostock", fb)
        df = f.get_daily("000001")
        assert not df.empty
        assert fb.calls == [], "主源成功时备选不应被调用"
