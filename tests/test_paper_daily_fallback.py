"""每日兜底脚本的单元测试（全部离线：不打真实 HTTP，不起服务）

为什么要测
----------
这个脚本由外部调度器(harness)每天调用, 是"服务没起也能把今天的账补上"的兜底。
它的判定逻辑若出错, 两种后果都是静默的:
  - 该补跑时判成"已处理" → 那天的账悄悄缺失
  - 不该补跑时判成"未处理" → 重复推进(对同一个交易日撮合两次)

此前只有一次真实环境的手工实测(周六/幂等/拉起), 判定逻辑本身没有锁。
CI 里跑不起真实服务, 所以把**纯逻辑**拆成可单测的函数:
  - `today_handled`          今天是否已处理
  - `run_exit_code`          执行结果 → 退出码
  - `extract_port` / `parse_run_hint`  参数与 --port 解析

HTTP 与进程拉起(`ensure_server`)不在这里测 —— 它们需要真实端口/进程,
由 experiments 层的真实环境实测覆盖(见 docs/OPERATIONS.md §1.3)。
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TZ = ZoneInfo("Asia/Shanghai")


@pytest.fixture(scope="module")
def fb():
    """加载 scripts/paper_daily_fallback.py(导入无副作用, 不起服务)

    脚本 import 同目录的 watchdog_ping —— 真实执行时 sys.path[0] 是 scripts/,
    测试里手动补上这个路径, 模拟与真实执行一致的环境。
    """
    scripts_dir = str(REPO_ROOT / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    path = REPO_ROOT / "scripts" / "paper_daily_fallback.py"
    assert path.is_file(), f"缺少兜底脚本 {path}"
    spec = importlib.util.spec_from_file_location("_paper_daily_fallback", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def iso(dt: datetime) -> str:
    return dt.isoformat()


# ==================== "今天已处理" 判定 ====================


class TestTodayHandled:
    def test_run_today_ok_counts(self, fb):
        now = datetime.now(TZ)
        assert fb.today_handled(iso(now), now) is True

    def test_run_today_skipped_counts(self, fb):
        """非交易日的 skipped 也算已处理 —— 那天本来就不该推进"""
        now = datetime.now(TZ)
        assert fb.today_handled(iso(now), now, last_status="skipped") is True

    def test_run_today_error_counts(self, fb):
        """失败的已由告警通道提醒, 兜底不重试(避免与内置调度撞车)"""
        now = datetime.now(TZ)
        assert fb.today_handled(iso(now), now, last_status="error") is True

    def test_yesterday_does_not_count(self, fb):
        now = datetime.now(TZ)
        yesterday = now - timedelta(days=1)
        assert fb.today_handled(iso(yesterday), now) is False

    def test_empty_last_run(self, fb):
        now = datetime.now(TZ)
        assert fb.today_handled("", now) is False

    def test_garbage_timestamp_does_not_count(self, fb):
        now = datetime.now(TZ)
        assert fb.today_handled("这不是时间", now) is False

    def test_naive_timestamp_with_today_date_counts(self, fb):
        """旧持久化的 naive 时间戳只要日期是今天就算(比对的是日期部分)"""
        naive = datetime.now(TZ).replace(tzinfo=None).isoformat()
        now = datetime.now(TZ)
        assert fb.today_handled(naive, now) is True

    def test_future_timestamp_counts(self, fb):
        """时钟回拨/别的机器写的未来时间: 日期是今天就算已处理, 不重复推进"""
        now = datetime.now(TZ)
        assert fb.today_handled(iso(now + timedelta(hours=3)), now) is True

    def test_cross_midnight_run_counts(self, fb):
        """**回归锁**（真 bug）: 21:17 的时间戳 +3h = 次日 00:17, 跨了午夜

        字符串前缀比对(==)会判 False -> 兜底重复推进同一交易日。
        正确语义: run_date **>=** today 即已处理(未来比"没有记录"更接近已处理,
        重复撮合的风险大于漏跑)。本测试用固定的跨午夜组合, 不依赖当前时刻。
        """
        now = datetime(2026, 10, 10, 23, 0, tzinfo=TZ)
        next_morning = datetime(2026, 10, 11, 0, 30, tzinfo=TZ)
        assert fb.today_handled(iso(next_morning), now) is True
        # 反向: 昨天的记录在"今天 23:00"仍不算已处理(今天还没跑)
        yesterday = datetime(2026, 10, 9, 15, 30, tzinfo=TZ)
        assert fb.today_handled(iso(yesterday), now) is False

    def test_date_like_garbage_does_not_count(self, fb):
        """形如日期但非法(2026-13-99)不算已处理 -> 走补跑(幂等拦重复)"""
        now = datetime.now(TZ)
        assert fb.today_handled("2026-13-99", now) is False


# ==================== 执行结果 → 退出码 ====================


class TestRunExitCode:
    def test_ok_is_zero(self, fb):
        assert fb.run_exit_code({"status": "ok"}) == 0

    def test_skipped_is_zero(self, fb):
        assert fb.run_exit_code({"status": "skipped"}) == 0

    def test_error_is_one(self, fb):
        assert fb.run_exit_code({"status": "error", "error": "x"}) == 1

    def test_unknown_status_is_one(self, fb):
        """未知状态宁可当失败 —— 兜底的退出码是外部调度器唯一的信号源"""
        assert fb.run_exit_code({}) == 1
        assert fb.run_exit_code({"status": "什么鬼"}) == 1


# ==================== --port 解析 ====================


class TestExtractPort:
    def test_default_port(self, fb):
        assert fb.extract_port([]) == fb.DEFAULT_PORT

    def test_explicit_port(self, fb):
        assert fb.extract_port(["--port", "9000"]) == 9000

    def test_port_before_other_args(self, fb):
        assert fb.extract_port(["--port", "9000", "--x"]) == 9000

    def test_missing_value_returns_default(self, fb):
        """--port 后面没有值: 用默认端口, 而不是 IndexError 崩掉"""
        assert fb.extract_port(["--port"]) == fb.DEFAULT_PORT

    def test_non_numeric_returns_default(self, fb):
        """--port abc: 用默认端口(并可附带提示), 而不是崩掉"""
        assert fb.extract_port(["--port", "abc"]) == fb.DEFAULT_PORT


# ==================== 模块级安全 ====================


def test_import_has_no_side_effects():
    """重新执行兜底脚本不得改全局告警(否则会污染整个测试会话)

    生成器脚本(experiments/generate_holidays.py)同样有这把锁 ——
    那边曾因模块级 filterwarnings 修过一次。
    """
    import warnings

    spec = importlib.util.spec_from_file_location(
        "_pfb_side_effect_probe",
        REPO_ROOT / "scripts" / "paper_daily_fallback.py",
    )
    module = importlib.util.module_from_spec(spec)
    before = warnings.filters[:]
    spec.loader.exec_module(module)
    assert warnings.filters == before


def test_default_port_is_not_8000(fb):
    """8000 被本机另一个常驻应用(everos)占用 —— 实测踩过, 不许回退"""
    assert fb.DEFAULT_PORT != 8000


class TestPingWiring:
    """main() 的两条退出路径都必须 ping 死信开关

    为什么值得锁: "已处理"路径若不 ping, 长假多天不 ping 会被监控服务**误报**;
    ping 若影响退出码, 监控挂了会把"账补上了"搞成"调度器以为失败"。
    这里 mock 掉 HTTP 与 ping 本体, 只验证**接线**。
    """

    @staticmethod
    def _run_main(monkeypatch, fb, *, already, run_status="ok"):
        """跑 main(): mock 网络/环境, 返回 (退出码, ping 调用列表)"""
        calls = []
        sched = {
            "running": True,
            "last_run_at": iso(datetime.now(TZ)) if already else "",
            "last_status": "skipped" if already else "",
        }
        monkeypatch.setattr(fb, "server_alive", lambda: True)
        monkeypatch.setattr(
            fb,
            "http_json",
            lambda path, method="GET", payload=None, timeout=30: (
                sched if "schedule" in path and method == "GET" else {"status": run_status}
            ),
        )
        monkeypatch.setattr(fb, "ping_quiet", lambda url, ok=True: calls.append((url, ok)))
        monkeypatch.setenv("STOCK_PING_URL", "https://hc-ping.com/xyz")
        return fb.main(), calls

    def test_already_path_pings_ok(self, monkeypatch, fb):
        code, calls = self._run_main(monkeypatch, fb, already=True)
        assert code == 0
        assert calls == [("https://hc-ping.com/xyz", True)], "已处理也必须报平安"

    def test_run_path_pings_with_status(self, monkeypatch, fb):
        code, calls = self._run_main(monkeypatch, fb, already=False, run_status="ok")
        assert code == 0
        assert calls == [("https://hc-ping.com/xyz", True)]

    def test_run_error_pings_fail(self, monkeypatch, fb):
        """失败要 ping /fail —— 让监控服务立即告警, 而不是等超时"""
        code, calls = self._run_main(monkeypatch, fb, already=False, run_status="error")
        assert code == 1
        assert calls == [("https://hc-ping.com/xyz", False)]

    def test_unconfigured_ping_still_exits_zero(self, monkeypatch, fb):
        """没配 STOCK_PING_URL 时: 不 ping、退出码不受影响(兜底本身最重要)"""
        sched = {"running": True, "last_run_at": iso(datetime.now(TZ)), "last_status": "ok"}
        monkeypatch.setattr(fb, "server_alive", lambda: True)
        monkeypatch.setattr(fb, "http_json", lambda *a, **k: sched)
        monkeypatch.delenv("STOCK_PING_URL", raising=False)
        monkeypatch.setattr(
            fb, "ping_quiet", lambda url, ok=True: pytest.fail("未配置时不应尝试 ping")
        )
        assert fb.main() == 0
