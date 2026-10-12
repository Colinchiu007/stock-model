"""RetryError 穿透数据层降级链的回归测试（2026-10-11 真实故障）

现象
----
用户在前端点「个股分析」(000001)，接口返回:
    {"error": "RetryError: RetryError[<Future at 0x... state=finished raised RuntimeError>]"}
服务端日志同时显示 3 次 [WinError 10054] 远程主机强迫关闭连接（baostock 服务器周末不稳）。

根因（两个叠加）
--------------
1. tenacity 重试耗尽抛 ``RetryError``，它**不是** RuntimeError/ConnectionError 的子类 ——
   ``_execute_with_fallback`` 的降级判定 ``_is_connection_error`` 靠
   ``_CONNECTION_ERROR_PATTERNS`` 匹配错误文本/类名，``RetryError`` 的文本
   只有 "RetryError[<Future ...>]"，**不含任何连接字样** → 不降级、直接穿透。
2. RetryError 的 ``str()`` 把真实根因（内层异常）藏在 ``<Future at 0x...>``
   后面 —— 用户看到的是一串无意义文本，不知道是"数据源连接被拒"。

本项目复盘文档记录过同款（webhook 层漏捕 RetryError），数据层漏了 —— 同根因第 2 次。

修复后的契约（本文件锁定）
------------------------
1. ``_is_connection_error(RetryError(内层连接异常))`` 必须为 True → 触发降级
2. 内层异常必须能被解出（``unwrap_retry_error``），错误呈现给用户的是
   "RuntimeError: <真实原因>" 而不是 "<Future at 0x...>"
3. 降级链: 主源 RetryError → 备选源被尝试
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# tenacity 是项目依赖(fetcher 在用)
from tenacity import RetryError, retry, stop_after_attempt  # noqa: E402


def _always_failing():
    """模拟数据源方法: tenacity 重试 3 次后抛 RetryError(内层是连接类 RuntimeError)"""

    @retry(stop=stop_after_attempt(3))
    def _inner():
        raise RuntimeError("远程主机强迫关闭了一个现有的连接")

    try:
        _inner()
    except RetryError as e:
        return e
    raise AssertionError("unreachable")  # pragma: no cover


@pytest.fixture(scope="module")
def fetcher_cls():
    """加载 StockDataFetcher(导入无副作用)"""
    spec = importlib.util.spec_from_file_location(
        "_fetcher", REPO_ROOT / "src" / "stock_model" / "data" / "fetcher.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("_fetcher", module)
    spec.loader.exec_module(module)
    return module.StockDataFetcher


class TestRetryErrorIsConnectionError:
    def test_retry_error_with_connection_inner_is_connection_error(self, fetcher_cls):
        """RetryError(内层=连接类 RuntimeError) 必须被判为可降级"""
        f = fetcher_cls(source="akshare")
        err = _always_failing()
        assert f._is_connection_error(err) is True, (
            "RetryError 文本里没有连接字样, 但内层是连接类错误 —— "
            "不识别它就不会触发降级(2026-10-11 真实故障)"
        )

    def test_retry_error_with_non_connection_inner_is_not(self, fetcher_cls):
        """内层是非连接错误(如 ValueError)时**不该**降级 —— 保持既有语义"""
        f = fetcher_cls(source="akshare")

        @retry(stop=stop_after_attempt(2))
        def _inner():
            raise ValueError("参数错误")

        try:
            _inner()
        except RetryError as e:
            err = e
        assert f._is_connection_error(err) is False


class TestUnwrapRetryError:
    def test_unwrap_returns_inner_exception(self, fetcher_cls):
        err = _always_failing()
        inner = fetcher_cls.unwrap_retry_error(err)
        assert isinstance(inner, RuntimeError)
        assert "远程主机" in str(inner)

    def test_unwrap_non_retry_error_is_identity(self, fetcher_cls):
        """非 RetryError 原样返回(调用方不必分支判断)"""
        v = ValueError("x")
        assert fetcher_cls.unwrap_retry_error(v) is v


class TestFallbackChain:
    def test_retry_error_triggers_fallback(self, fetcher_cls, monkeypatch):
        """主源抛 RetryError(连接类) → 必须尝试备选源(而不是穿透)"""
        # 备选源构造也 patch 掉: 真实 BaostockSource() 要 login,
        # 凌晨数据源全挂时会拖死/失败, 让断言"应有备选"变成环境依赖
        # (2026-10-13 实测: 该假设在 baostock 整体抖动时不成立)。
        fake_primary = MagicMock()
        fake_fallback = MagicMock()
        fake_fallback.name = "baostock"
        with patch.object(
            fetcher_cls,
            "_create_source",
            side_effect=lambda name: fake_primary if name == "akshare" else fake_fallback,
        ):
            f = fetcher_cls(source="akshare")
        assert f._fallback_instance is not None, "akshare 应有 baostock 备选"

        calls: list[str] = []

        class FakeSource:
            name = "fake"

            def get_daily(self, *a, **k):
                calls.append("fake")
                return pd.DataFrame({"close": [1.0]})

        # 主源方法抛 RetryError; 备选源换假源
        monkeypatch.setattr(
            f._source_instance,
            "get_daily",
            lambda *a, **k: (_ for _ in ()).throw(_always_failing()),
        )
        monkeypatch.setattr(f, "_fallback_instance", FakeSource())
        df = f._execute_with_fallback("get_daily", "000001")
        assert calls == ["fake"], "备选源必须被尝试"
        assert not df.empty

    def test_both_fail_raises_with_real_reason(self, fetcher_cls, monkeypatch):
        """两个源都失败时, 报错信息必须包含**真实根因**(内层异常文本)"""
        f = fetcher_cls(source="akshare")

        class FakeSource:
            name = "fake"

            def get_daily(self, *a, **k):
                raise ValueError("备选也不行")

        monkeypatch.setattr(
            f._source_instance,
            "get_daily",
            lambda *a, **k: (_ for _ in ()).throw(_always_failing()),
        )
        monkeypatch.setattr(f, "_fallback_instance", FakeSource())
        with pytest.raises(RuntimeError) as e:
            f._execute_with_fallback("get_daily", "000001")
        msg = str(e.value)
        assert "远程主机" in msg, f"错误信息必须含真实根因, 实际: {msg!r}"
        assert "Future at 0x" not in msg, "不应把 RetryError 的 <Future> 文本抛给用户"
