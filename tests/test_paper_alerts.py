"""模拟盘定时失败告警测试

为什么需要
----------
定时运行的**最后一块**不是"每天自动跑", 而是"跑挂了会有人知道"。
没有告警的自动化只做了一半: 界面/接口上的 ``last_error`` 摆在那儿,
但没人会天天去看 —— 实测缺口就在这(见 ``docs/HANDOVER.md`` TD-08)。

设计要点（三条都锁住了）
------------------------
1. **复用已有通道**, 不为告警另起一套传输 —— 通道 ``send`` 的 ``result`` 参数
   改成可选, 系统告警传 ``None``。
2. **告警节流**: 首次失败必发, 之后每 N 次发一次, 失败→成功恢复时也发一条。
   全都发会刷屏, 刷屏的下一步就是被静音 —— 那才是真正的"失败被静默"。
3. **告警失败不影响被监控的任务**: 通道挂了不该把定时运行也带崩,
   但原因必须留在 status 里(不能静默)。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from stock_model.config.settings import NotifySettings
from stock_model.notify import (
    ConsoleChannel,
    FileChannel,
    SignalNotifier,
    WebhookChannel,
    build_alert_notifier,
)
from stock_model.paper.scheduler import PaperScheduler, ScheduleState

CST = timezone(timedelta(hours=8))


def at(year: int, month: int, day: int, hour: int = 15, minute: int = 30) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=CST)


def register(sched: PaperScheduler, account_id: str = "default") -> ScheduleState:
    state = ScheduleState(account_id=account_id)
    sched._states[account_id] = state
    return state


@pytest.fixture
def sched(tmp_path):
    s = PaperScheduler(
        schedule_file=tmp_path / "schedule.json",
        holiday_file=tmp_path / "no-holidays.json",
    )
    s.set_runner(lambda account_id, days: {"status": "ok", "steps": days, "persisted": True})
    yield s
    s.shutdown()


# ==================== 通道接口的泛化 ====================


class TestChannelAcceptsAlert:
    """``result`` 必须可选 —— 否则告警只能伪造一个 StrategyResult, 很脏"""

    def test_console_channel_without_result(self, capsys):
        ConsoleChannel().send("⚠️ 定时任务失败", None)
        assert "定时任务失败" in capsys.readouterr().out

    def test_file_channel_writes_alert_record(self, tmp_path):
        path = tmp_path / "alerts.jsonl"
        FileChannel(str(path)).send("⚠️ 定时任务失败", None)

        record = json.loads(path.read_text(encoding="utf-8").strip())
        assert record["level"] == "alert"
        assert record["message"] == "⚠️ 定时任务失败"
        assert "symbol" not in record, "告警记录不该混入信号字段"

    def test_signal_record_shape_unchanged(self, tmp_path):
        """带 result 时仍是原来的信号记录(不能因为泛化改坏既有行为)"""
        from stock_model.strategy.base import ActionType, StrategyResult

        path = tmp_path / "signals.jsonl"
        result = StrategyResult(
            symbol="000001", action=ActionType.BUY, confidence=0.8, reason="测试"
        )
        FileChannel(str(path)).send("信号", result)

        record = json.loads(path.read_text(encoding="utf-8").strip())
        assert record["symbol"] == "000001"
        assert record["action"] == "buy"

    def test_notifier_notify_message_uses_all_channels(self, capsys):
        notifier = SignalNotifier()
        notifier.add_channel(ConsoleChannel())
        notifier.notify_message("系统告警: 定时任务失败")
        assert "系统告警" in capsys.readouterr().out

    def test_notifier_survives_broken_channel(self):
        """一个通道炸了不能影响其它通道, 也不能抛给调用方"""

        class Broken:
            def send(self, message, result=None):
                raise RuntimeError("通道坏了")

        notifier = SignalNotifier()
        notifier.add_channel(Broken())
        notifier.add_channel(ConsoleChannel())
        notifier.notify_message("告警")  # 不应抛异常

    def test_webhook_unreachable_does_not_raise(self):
        """webhook 不可达必须被吞掉并记日志

        回归保护: httpx 的 ConnectError 继承自 HTTPError 而**不是** OSError,
        原来的 `except (OSError, RuntimeError, TimeoutError)` 接不住它 ——
        "通知失败"会升级成"定时任务失败"。
        (同类漏捕本项目已栽过一次: tenacity.RetryError 也不是 RuntimeError。)
        端口 1 必然连不上, 故本测试不需要网络。
        """
        channel = WebhookChannel(url="http://127.0.0.1:1/webhook", timeout=1)
        channel.send("告警", None)  # 不应抛异常


class TestAlertNotifierAssembly:
    """按配置装配 —— 并且"没配"这件事要看得见"""

    def test_console_only_by_default(self):
        notifier = build_alert_notifier(NotifySettings(console=True, webhook_url=None))
        assert "ConsoleChannel" in notifier.description

    def test_webhook_url_is_masked(self):
        """状态里只回显主机名 —— webhook URL 含 access_token, 属凭据"""
        notifier = build_alert_notifier(
            NotifySettings(
                console=False,
                webhook_url="https://oapi.dingtalk.com/robot/send?access_token=SECRET123",
            )
        )
        assert "oapi.dingtalk.com" in notifier.description
        assert "SECRET123" not in notifier.description

    def test_disabled_means_no_channels(self):
        notifier = build_alert_notifier(NotifySettings(enabled=False))
        assert "未配置" in notifier.description

    def test_nothing_configured_is_reported(self):
        notifier = build_alert_notifier(NotifySettings(console=False, webhook_url=None))
        assert notifier.description == "(未配置通知通道)"


# ==================== 调度器侧: 什么时候发告警 ====================


class TestFailureAlerts:
    def test_first_failure_alerts(self, sched):
        sent: list[str] = []
        sched.set_alerter(sent.append, description="test")

        def boom(account_id, days):
            raise RuntimeError("数据源挂了")

        sched.set_runner(boom)
        state = register(sched)
        sched.run_now("default", now=at(2026, 10, 9))

        assert len(sent) == 1, "首次失败必须立刻告警"
        assert "数据源挂了" in sent[0]
        assert "连续失败: 1 次" in sent[0]
        assert state.alert_count == 1

    def test_alert_is_throttled_but_escalates(self, sched):
        """连续失败: 首次 + 每 N 次 —— 并带上累计次数, 升级趋势仍可见"""
        sent: list[str] = []
        sched.set_alerter(sent.append, description="test", every_n_failures=3)

        def boom(account_id, days):
            raise RuntimeError("x")

        sched.set_runner(boom)
        register(sched)
        for _ in range(7):
            sched.run_now("default", now=at(2026, 10, 9))

        assert len(sent) == 3, f"7 次失败应告警 3 次(1/3/6), 实际 {len(sent)}"
        assert "连续失败: 6 次" in sent[-1]

    def test_no_alert_on_skip(self, sched):
        """非交易日跳过不告警 —— 那不是故障, 天天周末告警会让人静音通知"""
        sent: list[str] = []
        sched.set_alerter(sent.append, description="test")
        register(sched)

        sched.run_now("default", now=at(2026, 10, 10))  # 周六
        assert sent == []

    def test_no_alert_on_healthy_run(self, sched):
        sent: list[str] = []
        sched.set_alerter(sent.append, description="test")
        register(sched)

        sched.run_now("default", now=at(2026, 10, 9))
        assert sent == []

    def test_recovery_alerts_once(self, sched):
        """失败→成功要发一条: 否则用户只知道坏过, 不知道什么时候好了"""
        sent: list[str] = []
        sched.set_alerter(sent.append, description="test")

        calls = {"n": 0}

        def flaky(account_id, days):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise RuntimeError("前两次失败")
            return {"status": "ok", "steps": 1, "persisted": True, "trades": 0}

        sched.set_runner(flaky)
        register(sched)
        for _ in range(4):
            sched.run_now("default", now=at(2026, 10, 9))

        recovery = [m for m in sent if "已恢复" in m]
        assert len(recovery) == 1, f"应恰好一条恢复通知, 实际 {len(recovery)}: {sent}"
        assert "此前连续失败: 2 次" in recovery[0]

    def test_persist_failure_also_alerts(self, sched):
        """「跑成功但没落盘」按失败处理 —— 也必须告警"""
        sent: list[str] = []
        sched.set_alerter(sent.append, description="test")
        sched.set_runner(
            lambda a, d: {"status": "ok", "steps": 1, "persisted": False, "persist_error": "磁盘满"}
        )
        register(sched)

        sched.run_now("default", now=at(2026, 10, 9))

        assert len(sent) == 1
        assert "磁盘满" in sent[0]


class TestAlertFailureDoesNotBreakJob:
    """告警通道挂了不能把定时运行带崩, 但也不能静默"""

    def test_broken_alerter_does_not_raise(self, sched):
        def broken_alerter(message):
            raise RuntimeError("webhook 挂了")

        sched.set_alerter(broken_alerter, description="broken")

        def boom(account_id, days):
            raise RuntimeError("业务失败")

        sched.set_runner(boom)
        state = register(sched)

        result = sched.run_now("default", now=at(2026, 10, 9))

        # 业务失败的记录照样完整, 调度不会因为告警炸掉
        assert result["status"] == "error"
        assert state.error_count == 1
        assert state.consecutive_failures == 1
        assert state.last_alert_error.startswith("RuntimeError")
        assert state.alert_count == 0, "没发出去的告警不该算数"

    def test_alert_error_is_surfaced_in_status(self, sched):
        sched.set_alerter(lambda m: (_ for _ in ()).throw(RuntimeError("通道坏了")))
        sched.set_runner(lambda a, d: (_ for _ in ()).throw(RuntimeError("业务失败")))
        register(sched)
        sched.run_now("default", now=at(2026, 10, 9))

        info = sched.status("default")
        assert any("告警发送失败" in w for w in info["warnings"]), info["warnings"]

    def test_status_exposes_channel_and_counts(self, sched):
        sent: list[str] = []
        sched.set_alerter(sent.append, description="ConsoleChannel + WebhookChannel(***)")
        sched.set_runner(lambda a, d: (_ for _ in ()).throw(RuntimeError("x")))
        register(sched)
        sched.run_now("default", now=at(2026, 10, 9))

        info = sched.status("default")
        assert info["alert_channel"] == "ConsoleChannel + WebhookChannel(***)"
        assert info["alert_count"] == 1
        assert info["last_alert_at"]

    def test_missing_channel_is_warned(self, sched):
        """没接告警通道必须显式提示 —— 否则用户以为"失败会通知我" """
        register(sched)
        info = sched.status("default")
        assert info["alert_channel"] == "(未接入告警通道)"
        assert any("未接入告警通道" in w for w in info["warnings"])

    def test_no_alerter_means_no_alert_but_job_runs(self, sched):
        sched.set_runner(lambda a, d: {"status": "ok", "steps": 1, "persisted": True})
        state = register(sched)
        result = sched.run_now("default", now=at(2026, 10, 9))
        assert result["status"] == "ok"
        assert state.alert_count == 0


class TestTradingDayAlertInteraction:
    """告警不该改变交易日/节流的既有语义"""

    def test_skipped_then_failed_still_alerts_first_failure(self, sched):
        sent: list[str] = []
        sched.set_alerter(sent.append, description="test")
        sched.set_runner(lambda a, d: (_ for _ in ()).throw(RuntimeError("x")))
        state = register(sched)

        sched.run_now("default", now=at(2026, 10, 10))  # 周六 → 跳过
        assert sent == []
        assert state.skipped_count == 1

        sched.run_now("default", now=at(2026, 10, 12))  # 周一 → 失败
        assert len(sent) == 1, "跳过不该影响'首次失败'的判定"
        assert state.consecutive_failures == 1

    def test_alert_every_zero_means_first_only(self, sched):
        sent: list[str] = []
        sched.set_alerter(sent.append, description="test", every_n_failures=0)
        sched.set_runner(lambda a, d: (_ for _ in ()).throw(RuntimeError("x")))
        register(sched)
        for _ in range(5):
            sched.run_now("default", now=at(2026, 10, 9))
        assert len(sent) == 1

    def test_holiday_shaped_date_does_not_alert(self, sched, tmp_path):
        """周末与节假日都不算故障"""
        (tmp_path / "holidays.json").write_text(json.dumps(["2026-10-12"]), encoding="utf-8")
        s = PaperScheduler(
            schedule_file=tmp_path / "s.json",
            holiday_file=tmp_path / "holidays.json",
        )
        s.set_runner(lambda a, d: {"status": "ok", "persisted": True})
        sent: list[str] = []
        s.set_alerter(sent.append, description="test")
        register(s)
        s.run_now("default", now=at(2026, 10, 12))
        assert sent == []
        s.shutdown()


class TestWeekdayGuardIsNotConfusedByAlerts:
    """确认告警路径没把 is_trading_day 的语义带偏(周一确实是交易日)"""

    def test_monday_is_trading_day(self):
        from stock_model.paper.scheduler import is_trading_day

        assert is_trading_day(date(2026, 10, 12)) is True
        assert is_trading_day(date(2026, 10, 10)) is False
