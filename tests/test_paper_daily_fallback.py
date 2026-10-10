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
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TZ = ZoneInfo("Asia/Shanghai")


@pytest.fixture(scope="module")
def fb():
    """加载 scripts/paper_daily_fallback.py(导入无副作用, 不起服务)"""
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
