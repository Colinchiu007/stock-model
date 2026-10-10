"""模拟盘定时调度

为什么需要
----------
模拟盘原本只能手动点「开始模拟」。要让它**每天自动跑一次**, 需要一个
后台调度, 而且要同时满足三件事: 不丢状态(``paper/store.py``)、
**不静默失败**、**重启后不会悄悄停掉**。

设计要点
--------
1. **交易日**: cron 只在周一~周五触发, 任务内再做一次显式判断(周末 +
   可选的节假日表)。非交易日**跳过并记录**, 而不是"悄悄什么都没做"。
2. **失败不静默**: 每次执行的结果(成功 / 失败 / 跳过)都写进可查询的
   status, 异常同时进日志。定时任务最危险的形态是"界面显示运行中,
   实际每轮都在报错"。
3. **不重入**: ``max_instances=1`` + ``coalesce=True``。上一轮没跑完时
   跳过本轮, 而不是两轮并发去改同一个账户。
4. **配置落盘**: 定时配置写 ``data/paper/schedule.json``, 进程重启后自动
   恢复。否则「每天自动跑」会在一次重启后静默失效 —— 用户以为还在跑。
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from stock_model.data.collector import MissingDependencyError
from stock_model.paper.store import write_json_atomic

if TYPE_CHECKING:
    from collections.abc import Callable

# 默认存储目录: <项目根>/data/paper/
DEFAULT_STORE_DIR = Path(__file__).resolve().parents[3] / "data" / "paper"
DEFAULT_SCHEDULE_FILE = DEFAULT_STORE_DIR / "schedule.json"
DEFAULT_HOLIDAY_FILE = DEFAULT_STORE_DIR / "holidays.json"

# A 股 15:00 收盘。当日 K 线要等收盘后才完整, 故默认收盘后 30 分钟执行。
MARKET_CLOSE_HOUR = 15
# 心跳过期阈值(天): 距上次任何活动(成功/失败/跳过)超过这个天数,
# status 给出"可能根本没在跑"的提示。刻意宽松 —— 周五到下周一隔 3 天、
# 长假隔 5 天都属正常; 心跳检测最怕误报, 误报的下一步就是被人静音。
HEARTBEAT_STALE_DAYS = 7
DEFAULT_HOUR = 15
DEFAULT_MINUTE = 30
DEFAULT_TIMEZONE = "Asia/Shanghai"
MIN_INTERVAL_MINUTES = 1


def is_trading_day(day: date, holidays: set[str] | None = None) -> bool:
    """是否 A 股交易日(粗判)

    **只排除周末与显式配置的节假日**, 不猜测调休/临时休市。
    真正的"今天有没有新 K 线"由引擎兜底: 数据没有推进时 ``step()``
    返回 ``no_more_data``, 不会在同一个交易日重复撮合。
    """
    if day.weekday() >= 5:
        return False
    return not (holidays and day.isoformat() in holidays)


def load_holidays(path: Path | None = None) -> set[str]:
    """读取节假日表(``data/paper/holidays.json``, 形如 ``["2026-10-01", ...]``)

    文件不存在时返回空集合(此时只排除周末)。**不静默假装有节假日表**:
    ``status()`` 会返回 ``holiday_calendar=False``, 调用方可感知。
    """
    p = path or DEFAULT_HOLIDAY_FILE
    if not p.exists():
        return set()

    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning(f"节假日表读取失败 {p}: {e}")
        return set()

    if not isinstance(data, list):
        logger.warning(f"节假日表格式异常(应为字符串数组), 已忽略: {p}")
        return set()
    return {str(x) for x in data}


@dataclass
class ScheduleState:
    """单个账户的调度状态

    这个对象就是「失败不静默」的载体: 执行了几次、失败几次、跳过几次、
    最后一次为什么失败, 全部可查。
    """

    account_id: str
    trigger_type: str = "daily"  # daily | interval
    hour: int = DEFAULT_HOUR
    minute: int = DEFAULT_MINUTE
    days: int = 1  # 每次推进几个交易日
    interval_minutes: int | None = None
    timezone: str = DEFAULT_TIMEZONE
    started_at: str = ""
    run_count: int = 0
    error_count: int = 0
    skipped_count: int = 0
    consecutive_failures: int = 0
    last_run_at: str = ""
    last_status: str = ""  # ok | error | skipped
    last_error: str = ""
    last_result: dict[str, Any] = field(default_factory=dict)
    # ---- 告警(失败提醒) ----
    alert_count: int = 0  # 实际发出的告警条数
    last_alert_at: str = ""
    last_alert_error: str = ""  # 告警**本身**失败的原因(不能静默)


class PaperScheduler:
    """模拟盘定时调度器(进程内单例, 与单 worker 约束一致)

    执行体与告警通道都由外部注入(``set_runner`` / ``set_alerter``), 这样调度器
    本身不必知道 API 层怎么建引擎、也不必认识 notify 模块 —— 只负责"什么时候跑、
    跑失败了叫一声"。好处是它可以被单独测试。
    """

    def __init__(
        self,
        schedule_file: Path | None = None,
        holiday_file: Path | None = None,
        alert_every_n_failures: int = 3,
    ) -> None:
        self._schedule_file = schedule_file or DEFAULT_SCHEDULE_FILE
        self._states: dict[str, ScheduleState] = {}
        self._scheduler: Any = None
        self._runner: Callable[[str, int], dict[str, Any]] | None = None
        self._alerter: Callable[[str], None] | None = None
        self._alerter_description = ""
        # 变更监听器(TD-06): 状态变化时回调(account_id, status)。
        # 调度器不认识 WebSocket —— 与 set_alerter 同模式, 只负责"变了就叫一下"。
        self._change_listener: Callable[[str, str], None] | None = None
        # 连续失败时每 N 次再提醒一次(首次必提醒); 0 = 只提醒首次
        self._alert_every = max(0, int(alert_every_n_failures))
        self._lock = threading.RLock()
        self._holidays = load_holidays(holiday_file)
        self._restore_error = ""

    # ==================== 执行体 / 告警通道 ====================

    def set_runner(self, runner: Callable[[str, int], dict[str, Any]] | None) -> None:
        """注入执行体: ``runner(account_id, days) -> {"status": ..., "persisted": bool}``"""
        self._runner = runner

    def set_alerter(
        self,
        alerter: Callable[[str], None] | None,
        *,
        description: str = "",
        every_n_failures: int | None = None,
    ) -> None:
        """注入告警通道: ``alerter(message)``

        Args:
            description: 人类可读的通道描述, 回显在 status 里 ——
                让"以为配了其实没配"这件事可见。
            every_n_failures: 连续失败时每 N 次再提醒一次(首次必提醒);
                不给则沿用构造时的默认值。节流策略跟着通道走, 因为它
                本质是"这个通道能承受多少噪音"。
        """
        self._alerter = alerter
        self._alerter_description = description
        if every_n_failures is not None:
            self._alert_every = max(0, int(every_n_failures))

    def set_change_listener(self, listener: Callable[[str, str], None] | None) -> None:
        """注入**状态变更**监听器: ``listener(account_id, status)``

        ok/error/skipped 三个出口都会触发; 没有监听器时完全无开销。
        与 set_alerter 同模式: 调度器不认识 WebSocket, 只负责叫一声。
        """
        self._change_listener = listener

    def _notify_change(self, account_id: str, status: str) -> None:
        """触发变更监听; **绝不抛** —— 推送失败不能影响调度结果"""
        listener = self._change_listener
        if listener is None:
            return
        try:
            listener(account_id, status)
        except Exception as e:
            logger.error(f"[paper-schedule] 变更通知失败: {e}")

    # ==================== 时区 ====================

    @staticmethod
    def _resolve_timezone(name: str) -> Any:
        """解析时区名; 缺 tzdata 时回退本地时区**并告警**(不静默)"""
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name)
        except (ImportError, KeyError) as e:
            logger.warning(f"时区 {name} 不可用({type(e).__name__}), 回退本地时区: {e}")
            return None

    def _now(self, tz_name: str) -> datetime:
        """当前时间(带时区; tz 不可用时退化为本地时间)"""
        return datetime.now(self._resolve_timezone(tz_name))

    # ==================== 启停 ====================

    def start(
        self,
        account_id: str = "default",
        *,
        hour: int | None = None,
        minute: int | None = None,
        days: int = 1,
        interval_minutes: int | None = None,
        timezone: str = DEFAULT_TIMEZONE,
        persist: bool = True,
    ) -> dict[str, Any]:
        """开启定时运行

        Args:
            account_id: 目标账户
            hour / minute: 每天几点执行(``interval_minutes`` 为 None 时生效)
            days: 每次推进几个交易日
            interval_minutes: 按间隔执行(分钟), 给定时忽略 hour/minute
            persist: 是否写入配置文件(恢复流程传 False, 避免重复写)

        Raises:
            MissingDependencyError: apscheduler 未安装
            ValueError: 参数不合法
        """
        days = max(1, int(days))

        with self._lock:
            state = self._states.get(account_id) or ScheduleState(account_id=account_id)

            if interval_minutes is not None:
                interval_minutes = int(interval_minutes)
                if interval_minutes < MIN_INTERVAL_MINUTES:
                    raise ValueError(f"间隔不能小于 {MIN_INTERVAL_MINUTES} 分钟")
                state.trigger_type = "interval"
                state.interval_minutes = interval_minutes
            else:
                state.trigger_type = "daily"
                state.interval_minutes = None
                state.hour = DEFAULT_HOUR if hour is None else int(hour)
                state.minute = DEFAULT_MINUTE if minute is None else int(minute)
                if not (0 <= state.hour <= 23):
                    raise ValueError(f"hour 必须在 0~23, 收到 {state.hour}")
                if not (0 <= state.minute <= 59):
                    raise ValueError(f"minute 必须在 0~59, 收到 {state.minute}")

            state.days = days
            state.timezone = timezone
            state.started_at = datetime.now().isoformat()

            # 先确保依赖可用 —— 缺 apscheduler 时这里抛 MissingDependencyError,
            # 而不是让 CronTrigger 的 import 抛一个裸 ImportError 出去
            # (裸的不会被调用方的 except MissingDependencyError 接住, 会变成 500)
            scheduler = self._ensure_scheduler()

            if state.trigger_type == "interval":
                trigger: Any = "interval"
                trigger_kwargs: dict[str, Any] = {"minutes": state.interval_minutes}
            else:
                from apscheduler.triggers.cron import CronTrigger

                # 周末由 cron 结构性排除(不依赖任务内的判断)
                trigger = CronTrigger(
                    day_of_week="mon-fri",
                    hour=state.hour,
                    minute=state.minute,
                    timezone=self._resolve_timezone(timezone),
                )
                trigger_kwargs = {}

            scheduler.add_job(
                self._job,
                trigger=trigger,
                args=[account_id],
                id=self._job_id(account_id),
                name=f"模拟盘定时推进: {account_id}",
                replace_existing=True,
                max_instances=1,  # 上一轮没跑完就跳过本轮, 不并发改同一个账户
                coalesce=True,  # 休眠/停机错过多次只补跑一次
                misfire_grace_time=3600,
                **trigger_kwargs,
            )

            self._states[account_id] = state
            if persist:
                self._save_config()

            logger.info(
                f"[paper-schedule] 账户 {account_id} 定时任务已启动: {self._describe(state)}"
            )
            return self.status(account_id)

    def stop(self, account_id: str = "default", *, persist: bool = True) -> dict[str, Any]:
        """停止定时运行"""
        with self._lock:
            if self._scheduler is not None:
                self._scheduler.remove_job(self._job_id(account_id))
            self._states.pop(account_id, None)
            if persist:
                self._save_config()
            logger.info(f"[paper-schedule] 账户 {account_id} 定时任务已停止")
            return {"status": "stopped", "account_id": account_id}

    def shutdown(self) -> None:
        """关闭整个调度器(测试与进程退出用)"""
        with self._lock:
            if self._scheduler is not None:
                self._scheduler.shutdown(wait=False)
                self._scheduler = None
            self._states.clear()

    # ==================== 状态 ====================

    def status(self, account_id: str | None = None) -> dict[str, Any]:
        """查询调度状态

        无 ``account_id`` 时返回全部任务; 有则返回该账户。
        """
        with self._lock:
            if account_id is not None:
                state = self._states.get(account_id)
                if state is None:
                    return {
                        "account_id": account_id,
                        "running": False,
                        "restore_error": self._restore_error,
                    }
                return self._state_dict(state)
            return {
                "jobs": [self._state_dict(s) for s in self._states.values()],
                "count": len(self._states),
                "restore_error": self._restore_error,
            }

    def _state_dict(self, state: ScheduleState) -> dict[str, Any]:
        info = asdict(state)
        info["running"] = True
        info["schedule"] = self._describe(state)
        info["holiday_calendar"] = bool(self._holidays)
        info["next_run_time"] = self._next_run_time(state.account_id)
        info["alert_channel"] = self._alerter_description or "(未接入告警通道)"
        info["warnings"] = self._warnings(state)
        return info

    def _warnings(self, state: ScheduleState) -> list[str]:
        """把"配置上值得注意的点"显式暴露出来, 而不是只写在日志里"""
        warns: list[str] = []
        if state.trigger_type == "daily" and state.hour < MARKET_CLOSE_HOUR:
            warns.append(
                f"执行时间 {state.hour:02d}:{state.minute:02d} 早于收盘 "
                f"{MARKET_CLOSE_HOUR}:00, 当日 K 线可能不完整"
            )
        if not self._holidays:
            warns.append(
                "未加载节假日表(仅排除周末)。节假日当天引擎会因无新数据自动空转, "
                f"如需精确排除请提供 {DEFAULT_HOLIDAY_FILE.name}"
            )
        elif self._holiday_table_expired(state):
            covered = max(d[:4] for d in self._holidays)
            warns.append(
                f"节假日表已过期: 只覆盖到 {covered} 年, 今年的节假日不会被排除"
                f"(照常触发) —— 请重新生成: python experiments/generate_holidays.py"
            )
        if state.consecutive_failures >= 2:
            warns.append(f"已连续失败 {state.consecutive_failures} 次, 请查看 last_error")
        heartbeat = self._heartbeat_message(state)
        if heartbeat:
            warns.append(heartbeat)
        if self._alerter is None:
            # 这条很重要: 定时任务失败了却没人知道, 等于"自动化"只做了一半
            warns.append(
                "未接入告警通道 —— 失败只会进日志, 不会主动通知你。"
                "可在 .env 里配置 STOCK_NOTIFY_WEBHOOK_URL(钉钉/飞书/企业微信)"
            )
        if state.last_alert_error:
            warns.append(f"上一条告警发送失败: {state.last_alert_error}")
        return warns

    def _heartbeat_message(self, state: ScheduleState) -> str:
        """距上次**任何活动**超过阈值 → 提示"可能根本没在跑"

        为什么需要这条：告警只能证明"进程活着时出过错"，**证明不了"进程还活着"**。
        服务被停、机器关机 —— 这些都不会产生失败记录，`error_count` 不会涨，
        唯一会变旧的是 ``last_run_at``。没有人会天天去看这个字段，
        所以在 status/界面上显式说出来。

        为什么成功/失败/**跳过**都算活动：三条路径都会写 ``last_run_at``，
        "跳过"同样证明调度器活着并在判断交易日。

        **诚实边界**：这条提示只在"有人来查"时才可能出现 —— 进程死了不会有
        任何东西主动推送。要做到"死了也通知"，需要进程外的 watchdog（独立待办）。

        阈值刻意宽松（工作日 7 天而不是 1~3 天）：周五跑到下周一隔 3 天是**正常**的，
        遇上长假隔 5 天也正常 —— 心跳检测**最怕误报**，误报的下一步就是被人静音。
        """
        if not state.last_run_at:
            return ""  # 从未跑过: 刚开启属正常
        try:
            last = datetime.fromisoformat(state.last_run_at)
        except ValueError:
            return ""  # 坏时间戳不在这里报 —— 不能让一个坏字段把 status 弄挂
        if last.tzinfo is None:
            # 旧版本持久化的 naive 时间戳: 按任务自己的时区补齐再比较
            last = last.replace(tzinfo=self._resolve_timezone(state.timezone))
        now = self._now(state.timezone)
        if state.trigger_type == "interval":
            threshold = timedelta(minutes=max(3 * (state.interval_minutes or 1), 10))
        else:
            threshold = timedelta(days=HEARTBEAT_STALE_DAYS)
        gap = now - last
        if gap <= threshold:
            return ""

        total_minutes = int(gap.total_seconds() // 60)
        days, minutes = divmod(total_minutes, 24 * 60)
        human = f"{days} 天 {minutes} 分钟" if days else f"{total_minutes} 分钟"
        return (
            f"距上次执行已 {human}, 远超预期({threshold.days} 天内) —— "
            f"服务可能根本没在跑(进程被停/机器关机**不会**产生失败记录)。"
            f"上次活动: {state.last_run_at}"
        )

    def _holiday_table_expired(self, state: ScheduleState) -> bool:
        """节假日表是否已不覆盖"今年"

        为什么按**年份**判断，而不是"表里最晚那条日期是否已过去"：
        2026 年的表最晚一条是 ``2026-10-07``（国庆最后一天）—— 之后到年底本来
        就没有节假日了。按"最晚日期"判断的话，**10 月 8 日起会天天误报"已过期"**。
        实测就是这样踩到的：界面验证的截图里出现了这条误报，而当时的断言没抓到它
        （断言只查了"未加载节假日表"那条旧告警，没查"不该出现的新告警"）。
        教训已记入 HANDOVER 5.3。

        **能**检测：表没有覆盖到今年（跨年后最典型的失效，也是这个文件的实际生命周期）。
        **不能**检测：今年的表漏了某个节假日 —— 那要靠生成器的双源交叉验证
        （``experiments/generate_holidays.py``），静态检查拿不到真值。
        """
        if not self._holidays:
            return False
        years = {d[:4] for d in self._holidays if isinstance(d, str)}
        if not years:
            return False
        return max(years) < str(self._now(state.timezone).year)

    def _next_run_time(self, account_id: str) -> str:
        if self._scheduler is None:
            return ""
        try:
            job = self._scheduler.get_job(self._job_id(account_id))
            return job.next_run_time.isoformat() if job and job.next_run_time else ""
        except (AttributeError, KeyError):
            return ""

    @staticmethod
    def _job_id(account_id: str) -> str:
        return f"paper_{account_id}"

    @staticmethod
    def _describe(state: ScheduleState) -> str:
        if state.trigger_type == "interval":
            return f"每 {state.interval_minutes} 分钟推进 {state.days} 个交易日"
        return f"每个交易日 {state.hour:02d}:{state.minute:02d} 推进 {state.days} 个交易日"

    # ==================== 执行 ====================

    def run_now(self, account_id: str = "default", now: datetime | None = None) -> dict[str, Any]:
        """立刻执行一次

        与定时触发**走完全相同的代码路径**(交易日判断、失败记录、落盘检查),
        因此可以拿它做端到端验证 —— 手动跑得通, 定时才跑得通。

        Args:
            now: 仅测试用, 显式指定"当前时间"以验证交易日判断
        """
        return self._job(account_id, now=now)

    def _job(self, account_id: str, now: datetime | None = None) -> dict[str, Any]:
        """任务体: 交易日判断 → 执行 → 记录结果(含异常)"""
        with self._lock:
            state = self._states.get(account_id)
            if state is None:
                msg = f"账户 {account_id} 没有调度配置"
                logger.error(f"[paper-schedule] {msg}, 本次不执行")
                return {"status": "error", "error": msg}

            now = now or self._now(state.timezone)

            if not is_trading_day(now.date(), self._holidays):
                state.skipped_count += 1
                state.last_status = "skipped"
                state.last_run_at = now.isoformat()
                state.last_result = {"status": "skipped", "date": now.date().isoformat()}
                reason = "非交易日(周末或节假日)"
                logger.info(f"[paper-schedule] 账户 {account_id} {now.date()} {reason}, 跳过")
                self._notify_change(account_id, "skipped")
                return {"status": "skipped", "reason": reason, "date": now.date().isoformat()}

            if state.trigger_type == "daily" and now.hour < MARKET_CLOSE_HOUR:
                logger.warning(
                    f"[paper-schedule] 账户 {account_id} 在 "
                    f"{now.hour:02d}:{now.minute:02d} 执行, 早于收盘 "
                    f"{MARKET_CLOSE_HOUR}:00 —— 当日 K 线可能不完整"
                )

            if self._runner is None:
                # 未注入执行体 = 配置错误。必须显式失败, 否则就是"跑了个空"
                return self._record_failure(
                    state, now, RuntimeError("未注入执行体(set_runner 未被调用)")
                )

            try:
                result = self._runner(account_id, state.days) or {}
            except Exception as e:
                return self._record_failure(state, now, e)

            if result.get("persisted") is False:
                # 跑成功了但没落盘 —— 对定时运行而言等同于失败:
                # 下一轮会从旧状态继续, 用户看到的是"每天都在跑, 账却不动"
                return self._record_failure(
                    state,
                    now,
                    RuntimeError(f"状态落盘失败: {result.get('persist_error', '未知原因')}"),
                    result=result,
                )

            # 从连续失败中恢复: 先记下前值再清零, 否则"恢复了"这件事没人知道
            was_failing = state.consecutive_failures
            state.run_count += 1
            state.consecutive_failures = 0
            state.last_status = "ok"
            state.last_error = ""
            state.last_run_at = now.isoformat()
            state.last_result = {
                k: v for k, v in result.items() if k not in ("disclaimer", "signals", "orders")
            }
            logger.info(
                f"[paper-schedule] 账户 {account_id} 定时执行完成: "
                f"{result.get('steps', 0)} 步, 成交 {result.get('trades', 0)} 笔"
            )
            if was_failing:
                # 失败→成功 是状态变化, 值得一条通知: 否则用户只知道"坏过",
                # 不知道"什么时候好了"
                self._alert(
                    state,
                    now,
                    f"✅ 模拟盘定时执行已恢复\n账户: {account_id}\n"
                    f"此前连续失败: {was_failing} 次\n"
                    f"本次结果: {result.get('steps', 0)} 步, 成交 {result.get('trades', 0)} 笔",
                )
            self._notify_change(account_id, "ok")
            return {"status": "ok", "account_id": account_id, **state.last_result}

    def _record_failure(
        self,
        state: ScheduleState,
        now: datetime,
        exc: Exception,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """记录失败 —— 日志 + 可查询的 status 双写 + **主动告警**, 不允许静默"""
        state.error_count += 1
        state.consecutive_failures += 1
        state.last_status = "error"
        state.last_error = f"{type(exc).__name__}: {exc}"
        state.last_run_at = now.isoformat()
        state.last_result = result or {}
        logger.opt(exception=exc).error(
            f"[paper-schedule] 账户 {state.account_id} 定时执行失败"
            f"(连续第 {state.consecutive_failures} 次): {state.last_error}"
        )
        n = state.consecutive_failures
        # 告警节流: 首次必发; 之后每 N 次再发一次(默认 N=3)。
        # 为什么不一失败就发: 连着一周每天都失败会刷屏, 人会把通知静音 ——
        # 那才是真正的"失败被静默"。消息里带连续次数, 升级趋势仍可见。
        should_alert = n == 1 or (self._alert_every > 0 and n % self._alert_every == 0)
        if should_alert:
            self._alert(
                state,
                now,
                f"⚠️ 模拟盘定时执行失败\n账户: {state.account_id}\n"
                f"时间: {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"连续失败: {n} 次\n"
                f"原因: {state.last_error}\n"
                f"排查: GET /api/paper/schedule?account_id={state.account_id}",
            )
        self._notify_change(state.account_id, "error")
        return {"status": "error", "account_id": state.account_id, "error": state.last_error}

    def _alert(self, state: ScheduleState, now: datetime, message: str) -> None:
        """发出告警

        **告警失败绝不能影响被监控的任务** —— 通道挂了不该把定时运行也带崩,
        但也不能静默: 原因写进 ``state.last_alert_error`` 并打 error 日志,
        status 接口会回显。
        """
        if self._alerter is None:
            return
        try:
            self._alerter(message)
        except Exception as e:
            # 通道类型是任意可调用对象, 必须全部兜住 —— 告警炸了不能带崩调度
            state.last_alert_error = f"{type(e).__name__}: {e}"
            logger.error(f"[paper-schedule] 账户 {state.account_id} 告警发送失败: {e}")
            return
        state.alert_count += 1
        state.last_alert_at = now.isoformat()
        state.last_alert_error = ""

    # ==================== 配置持久化 ====================

    def _ensure_scheduler(self) -> Any:
        """惰性创建 APScheduler(缺依赖时显式报错, 不降级为"静默不跑")"""
        if self._scheduler is not None:
            return self._scheduler

        try:
            from apscheduler.schedulers.background import BackgroundScheduler
        except ImportError as err:
            raise MissingDependencyError(
                "模拟盘定时运行不可用: 未安装 apscheduler。请执行 "
                "`pip install apscheduler` 或 `pip install stock-model[schedule]`; "
                "也可用 POST /api/paper/step 手动推进。"
            ) from err

        self._scheduler = BackgroundScheduler(timezone=self._resolve_timezone(DEFAULT_TIMEZONE))
        self._scheduler.start()
        return self._scheduler

    def _save_config(self) -> None:
        jobs = {
            account_id: {
                "trigger_type": s.trigger_type,
                "hour": s.hour,
                "minute": s.minute,
                "days": s.days,
                "interval_minutes": s.interval_minutes,
                "timezone": s.timezone,
                "started_at": s.started_at,
            }
            for account_id, s in self._states.items()
        }
        try:
            write_json_atomic(self._schedule_file, {"version": 1, "jobs": jobs})
        except OSError as e:
            # 配置写不进去 = 重启后定时任务会消失, 属于必须让用户知道的问题
            logger.error(f"[paper-schedule] 定时配置落盘失败 {self._schedule_file}: {e}")

    def _load_config(self) -> dict[str, dict[str, Any]]:
        if not self._schedule_file.exists():
            return {}

        try:
            data = json.loads(self._schedule_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.error(f"[paper-schedule] 定时配置读取失败 {self._schedule_file}: {e}")
            return {}
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(jobs, dict):
            logger.error(f"[paper-schedule] 定时配置结构异常, 已忽略: {self._schedule_file}")
            return {}
        return jobs

    def restore(self) -> dict[str, Any]:
        """从配置文件恢复定时任务(进程重启后自动继续)

        没有配置文件时**不做任何事**(也不启动调度器) —— 否则每个只调用一次
        ``create_app()`` 的进程都会凭空起一个后台线程。
        """
        jobs = self._load_config()
        if not jobs:
            return {"restored": 0}

        restored = 0
        for account_id, cfg in jobs.items():
            if not isinstance(cfg, dict):
                logger.error(f"[paper-schedule] 账户 {account_id} 的配置格式异常, 已跳过")
                continue
            try:
                self.start(
                    account_id,
                    hour=cfg.get("hour"),
                    minute=cfg.get("minute"),
                    days=int(cfg.get("days", 1)),
                    interval_minutes=cfg.get("interval_minutes"),
                    timezone=str(cfg.get("timezone") or DEFAULT_TIMEZONE),
                    persist=False,
                )
                restored += 1
            except MissingDependencyError as e:
                # 用户配置过定时, 重启后却因缺依赖静默跑不起来 —— 必须显式报错
                self._restore_error = str(e)
                logger.error(f"[paper-schedule] 恢复定时任务失败: {e}")
                return {"restored": restored, "error": self._restore_error}
            except (ValueError, TypeError) as e:
                logger.error(f"[paper-schedule] 账户 {account_id} 的配置无法恢复: {e}")

        logger.info(f"[paper-schedule] 已恢复 {restored} 个定时任务")
        return {"restored": restored}


_scheduler_singleton: PaperScheduler | None = None
_singleton_lock = threading.Lock()


def get_scheduler() -> PaperScheduler:
    """取进程内调度器单例

    **单例是必要的**: 多 worker 下每个进程会各起一份调度器, 同一个账户
    会被重复推进。项目已用 ``_assert_single_worker`` 在启动时拒绝多 worker。
    """
    global _scheduler_singleton
    with _singleton_lock:
        if _scheduler_singleton is None:
            _scheduler_singleton = PaperScheduler()
        return _scheduler_singleton
