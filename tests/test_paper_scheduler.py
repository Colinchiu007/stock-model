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
from pathlib import Path
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

REPO_ROOT = Path(__file__).resolve().parents[1]

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

    def test_expired_table_is_warned(self, tmp_path):
        """表过期后 is_trading_day 会**静默**退回"只排周末" —— 必须提示

        否则跨年后节假日照常触发, 用户在界面上看到的是"成功", 与
        "这天本来就不该跑"完全是两回事。
        """
        last_year = datetime.now(CST).year - 1
        s = make_scheduler(tmp_path, holidays=[f"{last_year}-01-01", f"{last_year}-10-01"])
        s.set_runner(lambda a, d: {"status": "ok", "persisted": True})
        register(s)
        warns = s.status("default")["warnings"]
        assert any("过期" in w and str(last_year) in w for w in warns), warns

    def test_fresh_table_is_not_warned(self, tmp_path):
        """覆盖到未来的表不该被告警 —— 否则提示会变成噪音被人忽略"""
        s = make_scheduler(tmp_path, holidays=["2099-10-01"])
        s.set_runner(lambda a, d: {"status": "ok", "persisted": True})
        register(s)
        warns = s.status("default")["warnings"]
        assert not any("过期" in w for w in warns), warns
        assert s.status("default")["holiday_calendar"] is True

    def test_table_with_only_past_dates_this_year_is_not_expired(self, tmp_path):
        """**回归锁**：本年度的表, 最后一条假日已过去 ≠ 表过期

        真实事故: 2026 年的表最晚一条是 2026-10-07(国庆), 到年底再无节假日。
        第一版按"最晚日期是否已过去"判断, 于是从 2026-10-08 起**天天误报"已过期"**;
        而当时的界面断言没抓到 —— 它只查了"未加载节假日表"那条**旧**告警,
        没查"不该出现的新告警"。**截图抓到了, 断言没抓到。**
        """
        y = datetime.now(CST).year
        s = make_scheduler(tmp_path, holidays=[f"{y}-01-01", f"{y}-10-07"])
        s.set_runner(lambda a, d: {"status": "ok", "persisted": True})
        register(s)
        warns = s.status("default")["warnings"]
        assert not any("过期" in w for w in warns), f"误报过期: {warns}"

    def test_no_unexpected_warnings_for_fresh_setup(self, tmp_path):
        """健康配置下 warnings 应为空 —— 有告警框就该有真问题

        这条专门防"断言只查自己认识的那条告警"这个坑: 与其逐条列举不该出现的
        文案, 不如在**一切正常**时要求整个列表为空。
        """
        s = make_scheduler(tmp_path, holidays=["2099-10-01"])
        s.set_runner(lambda a, d: {"status": "ok", "persisted": True})
        s.set_alerter(lambda m: None, description="test")
        register(s)
        assert s.status("default")["warnings"] == [], s.status("default")["warnings"]


class TestCommittedHolidayCalendar:
    """锁住**已提交**的 data/paper/holidays.json

    为什么要锁数据文件本身: 这份表是"节假日不触发"的唯一依据, 手改错了
    (比如把真实交易日写进去)会导致**该跑的那天不跑**, 而且不会有任何报错。
    """

    @staticmethod
    def _days() -> list[str]:
        path = REPO_ROOT / "data" / "paper" / "holidays.json"
        assert path.is_file(), f"缺少 {path}（跑 experiments/generate_holidays.py 生成）"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_well_formed(self):
        days = self._days()
        assert days, "节假日表为空 —— 等于没有"
        assert len(days) == len(set(days)), "有重复日期"
        assert days == sorted(days), "未按日期排序(人工核对与 diff 都会更难)"
        for d in days:
            date.fromisoformat(d)  # 格式非法会抛
        assert 10 <= len(days) <= 30, f"A 股一年节假日数量应在 10~30, 实际 {len(days)}"

    def test_no_weekend_in_table(self):
        """周末已由 is_trading_day 结构性排除, 表里出现周末说明数据有问题"""
        bad = [d for d in self._days() if date.fromisoformat(d).weekday() >= 5]
        assert not bad, f"节假日表里混入了周末: {bad}"

    def test_single_year_coverage(self):
        """本表按年生成 —— 混入多年说明生成脚本被改坏了"""
        years = {d[:4] for d in self._days()}
        assert len(years) == 1, f"节假日表覆盖多个年份: {sorted(years)}"

    def test_real_holiday_is_excluded(self):
        """行为断言: 表里的日期确实会被 is_trading_day 排除"""
        days = self._days()
        assert is_trading_day(date.fromisoformat(days[0]), set(days)) is False
        # 同期一个普通工作日必须仍是交易日 —— 防止"整年都被标成假日"
        probe = date.fromisoformat(days[0])
        while probe.isoformat() in set(days) or probe.weekday() >= 5:
            probe += timedelta(days=1)
        assert is_trading_day(probe, set(days)) is True

    # 黄金点: 已用**两个独立数据源**(baostock / akshare)逐点核对过
    # (2026-10-10 核对: 两边对每个日期的开/休市判断与下表完全一致)。
    # 完整核对靠 generate_holidays.py 的 cross_check(需要网络), CI 里跑不了,
    # 所以这里钉住若干真实日期 —— 手改表、或把真实交易日误写进来时会立刻红。
    GOLDEN_TRADING_DAYS = (
        "2026-01-05",  # 2026 首个交易日
        "2026-02-24",  # 春节假期后首个交易日(2/23 仍休市)
        "2026-10-08",  # 国庆假期后首个交易日
        "2026-10-09",  # 节后周五
    )
    GOLDEN_HOLIDAYS = (
        "2026-01-01",  # 元旦
        "2026-02-17",  # 春节
        "2026-06-19",  # 端午
        "2026-10-01",  # 国庆
    )

    def test_golden_spot_check(self):
        """真实交易日**不得**出现在假日表里 —— 那会让该跑的那天静默不跑

        这是本文件里唯一能抓"数据本身错了"的锁: 格式校验与行为断言都放它过去。
        """
        table = set(self._days())
        wrong = [d for d in self.GOLDEN_TRADING_DAYS if d in table]
        assert not wrong, f"这些是真实交易日, 却被写进了假日表: {wrong}"

        missing = [d for d in self.GOLDEN_HOLIDAYS if d not in table]
        assert not missing, f"这些是真实休市日, 却不在假日表里: {missing}"


class TestHolidayGeneratorFailClosed:
    """生成器最关键的安全属性: 两个源不一致时**拒绝写盘**

    把真实交易日误标为假日比"没有日历"更糟 —— 后者每天照跑、由引擎兜底,
    前者会让该跑的那天静默不跑。故这条必须上锁。
    """

    @staticmethod
    def _module():
        import importlib.util

        path = REPO_ROOT / "experiments" / "generate_holidays.py"
        assert path.is_file(), f"缺少生成脚本 {path}"
        spec = importlib.util.spec_from_file_location("_gen_holidays", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_module_import_has_no_side_effects(self):
        """导入本模块不能改动全局告警设置 —— 否则会污染整个测试会话"""
        import warnings as w

        before = w.filters[:]
        self._module()
        assert w.filters == before, "生成脚本在模块级改动了 warnings 过滤器"

    def test_disagreement_refuses_to_write(self):
        mod = self._module()
        with pytest.raises(RuntimeError) as e:
            mod.cross_check(2026, {"2026-01-05"}, {"2026-01-06"})
        msg = str(e.value)
        assert "拒绝生成" in msg
        assert "2026-01-05" in msg and "2026-01-06" in msg, "差异必须列出来才能人工核对"

    def test_agreement_passes(self):
        mod = self._module()
        mod.cross_check(2026, {"2026-01-05", "2026-01-06"}, {"2026-01-05", "2026-01-06"})

    def test_blocks_groups_consecutive_days(self):
        """连续日期要合成区间, 否则输出几十行没法人工核对"""
        mod = self._module()
        assert mod.blocks([date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5)]) == [
            "2026-10-01 ~ 2026-10-02",
            "2026-10-05",
        ]
        assert mod.blocks([]) == []


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
