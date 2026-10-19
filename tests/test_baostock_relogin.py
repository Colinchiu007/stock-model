"""baostock 会话失效自愈 + @retry reraise —— 回归测试

缺陷现象(2026-10-12 用户实测):
    前端「个股分析」→ `RetryError: RetryError[<Future at ... raised RuntimeError>]`
    但同一代码在新进程里跑同样的 fetcher **成功**(669 行)。

根因(两层叠加):
1. **会话失效无自愈**: BaostockSource._logged_in 是类变量, login 一次后永久
   True —— baostock 会话失效后(服务进程活了 29 小时)永远不会重新登录,
   查询走死连接 → WinError 10054 → 内层 @retry 耗尽 → RetryError。
   新进程 _logged_in=False → 重新 login → 成功(所以"脚本能跑、服务不能跑")。
2. **外层 @retry 无 reraise**: get_daily 的 @retry 重试耗尽后抛
   RetryError(包着内层), 把真实根因藏起来 —— 用户看到 "<Future at 0x...>"。

契约:
1. 查询遇连接错误(10054) → 重置登录态 + 重新 login + 重试查询(自愈)
2. fetcher 的所有 @retry 都有 reraise=True(耗尽后抛真实异常, 不包 RetryError)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.importorskip("baostock")


class _OneShotBoom:
    """最小的"永远失败"数据源: 任何方法都抛给定异常"""

    def __init__(self, exc: Exception | None = None):
        self.name = "boom"
        self._exc = exc or RuntimeError("远程主机强迫关闭了一个现有的连接")

    def get_daily(self, *a, **k):
        raise self._exc

    def __getattr__(self, item):
        def _raise(*a, **k):
            raise self._exc

        return _raise


def _stale_session_source():
    """模拟服务里那个"登录态为 True 但会话已死"的源实例"""
    from stock_model.data.sources import baostock_source as bs_mod
    from stock_model.data.sources.baostock_source import BaostockSource

    src = BaostockSource.__new__(BaostockSource)  # 不触发 _ensure_login
    BaostockSource._logged_in = True  # 假装登录过(死会话)
    return src, bs_mod


class TestStaleSessionSelfHeal:
    def test_connection_error_relogins_and_retries(self):
        """查询遇连接错误 → 重置登录态 + 重登录 + 重试查询(最终成功)"""

        src, bs_mod = _stale_session_source()

        ok_rs = MagicMock()
        ok_rs.error_code = "0"
        ok_rs.next.side_effect = [True, False]
        ok_rs.get_row_data.return_value = [
            "2026-01-02",
            "10",
            "10",
            "10",
            "10",
            "1000",
            "0",
            "0",
            "0",
        ]
        ok_rs.fields = [
            "date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
            "turn",
            "pctChg",
        ]

        calls = {"n": 0}

        def fake_query(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                # 第一次: 会话失效(远程主机强迫关闭)
                raise RuntimeError("[WinError 10054] 远程主机强迫关闭了一个现有的连接")
            return ok_rs

        with (
            patch.object(bs_mod.bs, "query_history_k_data_plus", side_effect=fake_query),
            patch.object(bs_mod.bs, "login") as mock_login,
        ):
            mock_login.return_value = MagicMock(error_code="0", error_msg="")
            df = src.get_daily("000001")

        mock_login.assert_called_once()  # 必须重新登录
        assert calls["n"] == 2, "重登后必须重试查询"
        assert not df.empty, "自愈后应拿到数据"

    def test_relogin_failure_raises_real_error(self):
        """重登也失败 → 抛**真实错误**(不是 RetryError)"""

        src, bs_mod = _stale_session_source()

        def always_dead(*a, **k):
            raise RuntimeError("[WinError 10054] 远程主机强迫关闭了一个现有的连接")

        with (
            patch.object(bs_mod.bs, "query_history_k_data_plus", side_effect=always_dead),
            patch.object(bs_mod.bs, "login") as mock_login,
        ):
            mock_login.return_value = MagicMock(error_code="0", error_msg="")
            with pytest.raises(RuntimeError, match="远程主机强迫关闭"):
                src.get_daily("000001")

        # 重试 3 次每次都会重登(会话反复失效), 至少调过一次 login
        assert mock_login.called


class TestRetryReraise:
    def test_all_fetcher_retries_have_reraise(self):
        """fetcher.py 所有 @retry 都必须 reraise=True(耗尽抛真实异常)"""
        src = (REPO_ROOT / "src" / "stock_model" / "data" / "fetcher.py").read_text(
            encoding="utf-8"
        )
        lines = src.splitlines()
        missing = []
        for idx, line in enumerate(lines):
            if "@retry(" not in line:
                continue
            window = " ".join(lines[idx : idx + 2])
            if "reraise=True" not in window:
                missing.append(f"第 {idx + 1} 行: {line.strip()[:80]}")
        assert not missing, "以下 @retry 缺 reraise=True:\n  " + "\n  ".join(missing)

    def test_get_daily_raises_real_error_not_retry_error(self):
        """行为锁: 源持续抛连接错误时, **最终异常保留真实根因**, 且不是 RetryError

        契约说明(2026-10-19 更新):
          原用例靠"没有备选源"来观察主源异常, 现已不成立 ——
          fetcher 现在有降级链(akshare/baostock/sina), baostock 失败会自动
          往下试, 全部失败后抛 RuntimeError, 其中内嵌各源真实原因。

          要保护的东西没变: 用户看到的必须是被解包的真实异常, 不是
          tenacity 的 RetryError 壳, 否则前端只会显示
          "RetryError[<Future ...>]"(2026-10-11 真实故障)。
        """
        from tenacity import RetryError

        from stock_model.data.fetcher import StockDataFetcher

        # 绕过 __init__: 只装主源, 降级链留空 —— 语义等价于"只有这一个源"
        f = object.__new__(StockDataFetcher)
        f.source = "baostock"
        f._source_instance = _OneShotBoom()
        f._primary_error = None
        f._fallback_instances = []
        f._fallback_instance = None
        f._init_assigned_fallback = None

        with pytest.raises(RuntimeError, match="远程主机强迫关闭") as e:
            f._execute_with_fallback("get_daily", "000001")
        assert not isinstance(e.value, RetryError)

    def test_retry_error_is_unwrapped_in_final_message(self):
        """锁: 各源都抛 RetryError 时, 最终错误里必须出现内层真实原因文本"""
        from stock_model.data.fetcher import StockDataFetcher

        f = object.__new__(StockDataFetcher)
        f.source = "baostock"
        f._source_instance = _OneShotBoom(RuntimeError("远程主机强迫关闭了一个现有的连接"))
        f._primary_error = None
        f._fallback_instances = []
        f._fallback_instance = None
        f._init_assigned_fallback = None

        with pytest.raises(RuntimeError, match="远程主机强迫关闭") as e:
            f._execute_with_fallback("get_daily", "000001")
        assert "RetryError" not in str(e.value), f"不该把 RetryError 壳抛给用户: {e.value}"
