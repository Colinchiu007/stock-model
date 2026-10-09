"""模拟盘定时调度测试

关注点不是「apscheduler 会不会按时触发」(那是它自己的事), 而是本项目
反复踩的三件事:

1. **失败不静默** —— 定时任务每轮都失败, 却没人知道
   (``test_scheduled_start_honesty.py`` 记录过同款缺陷: 界面显示"运行中"但没跑)
2. **不在非交易日跑** —— 周末/节假日空跑, 或在收盘前拿到不完整的 K 线
3. **重启后不静默失效** —— 配置落盘 + 恢复, 否则「每天自动跑」一次重启就没了
"""

from __future__ import annotations

import builtins
import json
import sys
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from stock_model.paper.scheduler import (
    DEFAULT_HOUR,
    DEFAULT_MINUTE,
    PaperScheduler,
    ScheduleState,
    is_trading_day,
    load_holidays,
)

# 2026-10-09 是交易日(周五), 10-10 是周六
FRIDAY = date(2026, 10, 9)
SATURDAY = date(2026, 10, 10)
SUNDAY = date(2026, 10, 11)
MONDAY = date(2026, 10, 12)

# 用固定 +08:00 而不是 ZoneInfo: 测试不该依赖系统时区库(tzdata)
CST = timezone(timedelta(hours=8))


def at(year: int, month: int, day: int, hour: int = 15, minute: int = 30) -> datetime:
    """带时区的"当前时间", 用于确定性地触发任务体"""
    return datetime(year, month, day, hour, minute, tzinfo=CST)


@contextmanager
def _block_apscheduler():
    """模拟未安装 apscheduler 的环境"""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("apscheduler"):
            raise ImportError("No module named 'apscheduler'")
        return real_import(name, *args, **kwargs)

    for mod in ("apscheduler", "apscheduler.schedulers", "apscheduler.schedulers.background"):
        sys.modules.pop(mod, None)
    with patch("builtins.__import__", fake_import):
        yield


def make_scheduler(tmp_path, holidays=None, config=True) -> PaperScheduler:
    """隔离的调度器: 配置文件与节假日表都在 tmp 下, 绝不碰真实 data/paper/"""
    holiday_file = tmp_path / "holidays.json"
    if holidays is not None:
        holiday_file.write_text(json.dumps(holidays), encoding="utf-8")
    schedule_file = tmp_path / "schedule.json"
    if config is False:
        schedule_file.unlink(missing_ok=True)
    return PaperScheduler(schedule_file=schedule_file, holiday_file=holiday_file)


@pytest.fixture
def sched(tmp_path):
    s = make_scheduler(tmp_path)
    s.set_runner(lambda account_id, days: {"status": "ok", "steps": days, "persisted": True})
    yield s
    s.shutdown()


@pytest.fixture
def live_sched(tmp_path):
    """需要真实 apscheduler 的用例"""
    pytest.importorskip("apscheduler")
    s = make_scheduler(tmp_path)
    s.set_runner(lambda account_id, days: {"status": "ok", "steps": days, "persisted": True})
    yield s
    s.shutdown()


def register(sched: PaperScheduler, account_id="default", **kwargs) -> ScheduleState:
    """直接登记状态(不建真实 job) —— 让"任务体行为"能在缺 apscheduler 时也测到"""
    state = ScheduleState(account_id=account_id, **kwargs)
    sched._states[account_id] = state
    return state


# ==================== 交易日判断 ====================


class TestTradingDay:
    def test_weekend_excluded(self):
        assert is_trading_day(FRIDAY) is True
        assert is_trading_day(SATURDAY) is False
        assert is_trading_day(SUNDAY) is False

    def test_weekday_included(self):
        assert is_trading_day(MONDAY) is True

    def test_configured_holiday_excluded(self):
        assert is_trading_day(MONDAY, holidays={"2026-10-12"}) is False
        assert is_trading_day(MONDAY, holidays={"2026-10-13"}) is True

    def test_empty_holidays_does_not_crash(self):
        assert is_trading_day(MONDAY, holidays=set()) is True
        assert is_trading_day(MONDAY, holidays=None) is True


class TestHolidayTable:
    def test_missing_file_returns_empty(self, tmp_path):
        """没有节假日表时只排除周末 —— 不算错, 但 status 必须能看出这一点"""
        assert load_holidays(tmp_path / "nope.json") == set()

    def test_reads_list(self, tmp_path):
        p = tmp_path / "h.json"
        p.write_text(json.dumps(["2026-10-12", "2026-10-13"]), encoding="utf-8")
        assert load_holidays(p) == {"2026-10-12", "2026-10-13"}

    def test_malformed_returns_empty(self, tmp_path):
        """坏文件不能让调度器起不来"""
        p = tmp_path / "h.json"
        p.write_text("{ 不是数组", encoding="utf-8")
        assert load_holidays(p) == set()

    def test_wrong_type_returns_empty(self, tmp_path):
        p = tmp_path / "h.json"
        p.write_text(json.dumps({"2026-10-12": "国庆"}), encoding="utf-8")
        assert load_holidays(p) == set()

    def test_status_reports_calendar_absence(self, tmp_path):
        """没加载节假日表必须显式暴露, 不能让人以为"已排除所有非交易日\""""
        s = make_scheduler(tmp_path)
        s.set_runner(lambda a, d: {"status": "ok", "persisted": True})
        register(s)
        info = s.status("default")
        assert info["holiday_calendar"] is False
        assert any("节假日" in w for w in info["warnings"])


# ==================== 任务体行为(失败不静默) ====================


class TestJobDoesNotFailSilently:
    def test_non_trading_day_skipped_and_recorded(self, sched):
        """周末不跑, 且"没跑"这件事要留痕"""
        state = register(sched)

        result = sched.run_now("default", now=at(2026, 10, 10))

        assert result["status"] == "skipped"
        assert state.skipped_count == 1
        assert state.run_count == 0
        assert state.last_status == "skipped"

    def test_runner_exception_is_recorded(self, sched):
        """执行体抛异常 → 状态必须变成 error 并留下原因"""

        def boom(account_id, days):
            raise RuntimeError("取数失败: baostock 连不上")

        sched.set_runner(boom)
        state = register(sched)

        result = sched.run_now("default", now=at(2026, 10, 9))

        assert result["status"] == "error"
        assert state.error_count == 1
        assert state.consecutive_failures == 1
        assert state.last_status == "error"
        assert "baostock 连不上" in state.last_error

    def test_consecutive_failures_accumulate_and_warn(self, sched):
        """连续失败要能一眼看出来 —— 单次失败常见, 天天失败是事故"""

        def boom(account_id, days):
            raise RuntimeError("x")

        sched.set_runner(boom)
        state = register(sched)
        for _ in range(3):
            sched.run_now("default", now=at(2026, 10, 9))

        assert state.consecutive_failures == 3
        assert any("连续失败" in w for w in sched.status("default")["warnings"])

    def test_persist_failure_counts_as_failure(self, sched):
        """跑成功但没落盘 = 失败

        下一轮会从旧状态继续, 用户看到的是"每天都在跑, 账却不动"。
        """
        sched.set_runner(
            lambda a, d: {"status": "ok", "steps": 1, "persisted": False, "persist_error": "磁盘满"}
        )
        state = register(sched)

        result = sched.run_now("default", now=at(2026, 10, 9))

        assert result["status"] == "error"
        assert state.last_status == "error"
        assert "磁盘满" in state.last_error
        assert state.run_count == 0, "没落盘的轮次不该被算作成功"

    def test_missing_runner_is_error_not_silent_noop(self, sched):
        """没注入执行体 → 显式失败, 而不是"每轮跑了个空\""""
        sched.set_runner(None)
        state = register(sched)

        result = sched.run_now("default", now=at(2026, 10, 9))

        assert result["status"] == "error"
        assert state.error_count == 1

    def test_unknown_account_is_reported(self, sched):
        result = sched.run_now("nobody", now=at(2026, 10, 9))
        assert result["status"] == "error"

    def test_success_resets_failure_streak(self, sched):
        calls = {"n": 0}

        def flaky(account_id, days):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("第一次失败")
            return {"status": "ok", "steps": 1, "persisted": True, "trades": 0}

        sched.set_runner(flaky)
        state = register(sched)
        sched.run_now("default", now=at(2026, 10, 9))
        assert state.consecutive_failures == 1

        result = sched.run_now("default", now=at(2026, 10, 9))
        assert result["status"] == "ok"
        assert state.consecutive_failures == 0
        assert state.last_error == ""
        assert state.run_count == 1

    def test_before_close_is_flagged(self, sched):
        """收盘前执行 → 当日 K 线可能不完整, 必须提示"""
        register(sched, hour=9, minute=35)
        warns = sched.status("default")["warnings"]
        assert any("早于收盘" in w for w in warns)


# ==================== 启停与查询 ====================


class TestStartStop:
    def test_start_daily_registers_job(self, live_sched):
        info = live_sched.start("default", hour=15, minute=30, days=1)

        assert info["running"] is True
        assert info["trigger_type"] == "daily"
        assert "15:30" in info["schedule"]
        assert info["next_run_time"], "已启动的任务必须有下次执行时间"

    def test_defaults_are_after_market_close(self, live_sched):
        """默认执行时间必须在收盘之后 —— 否则天天拿到不完整的 K 线"""
        info = live_sched.start("default")
        assert (info["hour"], info["minute"]) == (DEFAULT_HOUR, DEFAULT_MINUTE)
        assert info["hour"] >= 15

    def test_weekend_structurally_excluded_by_cron(self, live_sched):
        """周末由 cron 排除, 不依赖任务体内的判断"""
        live_sched.start("default", hour=15, minute=30)
        job = live_sched._scheduler.get_job("paper_default")
        assert "mon-fri" in str(job.trigger).lower()

    def test_start_interval(self, live_sched):
        info = live_sched.start("default", interval_minutes=30, days=2)
        assert info["trigger_type"] == "interval"
        assert info["interval_minutes"] == 30
        assert "30 分钟" in info["schedule"]

    def test_restart_replaces_job(self, live_sched):
        """重复开启不该叠出两个 job(那会一天跑两次)"""
        live_sched.start("default", hour=15, minute=30)
        live_sched.start("default", hour=16, minute=0)
        assert len(live_sched._scheduler.get_jobs()) == 1

    def test_invalid_params_rejected(self, live_sched):
        with pytest.raises(ValueError, match="hour"):
            live_sched.start("default", hour=25)
        with pytest.raises(ValueError, match="minute"):
            live_sched.start("default", minute=61)
        with pytest.raises(ValueError, match="间隔"):
            live_sched.start("default", interval_minutes=0)

    def test_stop_removes_job(self, live_sched):
        live_sched.start("default")
        assert len(live_sched._scheduler.get_jobs()) == 1

        live_sched.stop("default")
        assert live_sched._scheduler.get_jobs() == []
        assert live_sched.status("default")["running"] is False

    def test_status_of_unknown_account(self, live_sched):
        info = live_sched.status("nobody")
        assert info["running"] is False

    def test_missing_apscheduler_raises(self, tmp_path):
        """缺依赖必须抛错 —— 不能"启动了"却什么都没跑"""
        s = make_scheduler(tmp_path)
        s.set_runner(lambda a, d: {"status": "ok", "persisted": True})
        with _block_apscheduler(), pytest.raises(ImportError, match="apscheduler"):
            s.start("default")
        assert s._scheduler is None


# ==================== 配置落盘与恢复 ====================


class TestRestore:
    def test_config_written_on_start(self, live_sched, tmp_path):
        live_sched.start("default", hour=16, minute=5, days=2)
        cfg = json.loads((tmp_path / "schedule.json").read_text(encoding="utf-8"))
        assert cfg["jobs"]["default"]["hour"] == 16
        assert cfg["jobs"]["default"]["days"] == 2

    def test_config_cleared_on_stop(self, live_sched, tmp_path):
        live_sched.start("default")
        live_sched.stop("default")
        cfg = json.loads((tmp_path / "schedule.json").read_text(encoding="utf-8"))
        assert cfg["jobs"] == {}

    def test_no_config_means_no_scheduler(self, tmp_path):
        """没有配置文件时不启动调度器 —— 否则每个进程都会凭空起后台线程"""
        s = make_scheduler(tmp_path)
        s.set_runner(lambda a, d: {"status": "ok", "persisted": True})
        result = s.restore()
        assert result["restored"] == 0
        assert s._scheduler is None

    def test_restore_rebuilds_jobs(self, tmp_path):
        pytest.importorskip("apscheduler")
        first = make_scheduler(tmp_path)
        first.set_runner(lambda a, d: {"status": "ok", "persisted": True})
        first.start("default", hour=15, minute=30, days=1)
        first.start("second", interval_minutes=60)
        first.shutdown()

        second = make_scheduler(tmp_path)
        second.set_runner(lambda a, d: {"status": "ok", "persisted": True})
        result = second.restore()

        assert result["restored"] == 2
        assert second.status("default")["running"] is True
        assert second.status("second")["trigger_type"] == "interval"
        assert len(second._scheduler.get_jobs()) == 2
        second.shutdown()

    def test_restore_reports_missing_dependency(self, tmp_path):
        """已配置过定时, 重启后缺依赖 → 必须显式报错, 不能静默失效"""
        p = tmp_path / "schedule.json"
        p.write_text(
            json.dumps({"version": 1, "jobs": {"default": {"hour": 15, "minute": 30, "days": 1}}}),
            encoding="utf-8",
        )
        s = PaperScheduler(schedule_file=p, holiday_file=tmp_path / "h.json")
        s.set_runner(lambda a, d: {"status": "ok", "persisted": True})

        with _block_apscheduler():
            result = s.restore()

        assert result["restored"] == 0
        assert "apscheduler" in result["error"]
        assert "apscheduler" in s.status()["restore_error"]

    def test_corrupt_config_does_not_crash_startup(self, tmp_path, live_sched):
        (tmp_path / "schedule.json").write_text("{ 坏掉了", encoding="utf-8")
        result = live_sched.restore()
        assert result["restored"] == 0
