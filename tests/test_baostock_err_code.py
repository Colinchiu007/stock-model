"""baostock 错误码 10002007(网络接收错误)自愈 —— 追加测试

2026-10-12 22:10 用户实测: 选股扫描 000651/600519 报
`RuntimeError: baostock查询失败: 10002007 网络接收错误。`
(000001/000002/600036 成功) —— 说明:
1. 该错误从 `rs.error_code != "0"` 分支抛出, **在自愈包装之外**
   (PR #42 只包了 query_history_k_data_plus 调用本身)
2. `_SESSION_DEAD_PATTERNS` 没有 "10002007"/"网络接收错误" 字样, 接不住

契约:
1. 错误码 10002007 → 判定会话/网络失效 → 重登重试
2. `_is_session_dead` 识别 "网络接收错误"/"10002007"
3. 非网络类错误码(如 10002001 参数错) → **不**自愈, 原样抛
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.importorskip("baostock")


def _stale_session_source():
    from stock_model.data.sources import baostock_source as bs_mod
    from stock_model.data.sources.baostock_source import BaostockSource

    src = BaostockSource.__new__(BaostockSource)
    BaostockSource._logged_in = True
    return src, bs_mod


class TestErrorCodeSelfHeal:
    def test_error_code_10002007_relogins_and_retries(self):
        """错误码 10002007(网络接收错误) → 重登 + 重试查询(最终成功)"""

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

        bad_rs = MagicMock()
        bad_rs.error_code = "10002007"
        bad_rs.error_msg = "网络接收错误。"

        calls = {"n": 0}

        def fake_query(*a, **k):
            calls["n"] += 1
            return bad_rs if calls["n"] == 1 else ok_rs

        with (
            patch.object(bs_mod.bs, "query_history_k_data_plus", side_effect=fake_query),
            patch.object(bs_mod.bs, "login") as mock_login,
        ):
            mock_login.return_value = MagicMock(error_code="0", error_msg="")
            df = src.get_daily("000651")

        mock_login.assert_called_once(), "错误码 10002007 必须触发重登"
        assert calls["n"] == 2, "重登后必须重试查询"
        assert not df.empty, "自愈后应拿到数据"

    def test_non_network_error_code_not_retried(self):
        """非网络类错误码(如参数错) → 不重登, 原样抛 RuntimeError"""

        src, bs_mod = _stale_session_source()

        bad_rs = MagicMock()
        bad_rs.error_code = "10001001"
        bad_rs.error_msg = "股票代码格式错误"

        with (
            patch.object(bs_mod.bs, "query_history_k_data_plus", return_value=bad_rs),
            patch.object(bs_mod.bs, "login") as mock_login,
        ):
            mock_login.return_value = MagicMock(error_code="0", error_msg="")
            with pytest.raises(RuntimeError, match="10001001"):
                src.get_daily("000651")

        mock_login.assert_not_called(), "非网络错误不应重登"

    def test_is_session_dead_recognizes_network_receive(self):
        """_is_session_dead 识别 baostock 的网络类错误码/文本"""

        src, _ = _stale_session_source()
        assert src._is_session_dead(RuntimeError("baostock查询失败: 10002007 网络接收错误。"))
        assert src._is_session_dead(RuntimeError("[WinError 10054] 远程主机强迫关闭"))
        assert not src._is_session_dead(RuntimeError("baostock查询失败: 10001001 代码错"))
