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
from datetime import date, datetime
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


class PaperScheduler:
    """模拟盘定时调度器(进程内单例, 与单 worker 约束一致)

    执行体由外部注入(``set_runner``), 这样调度器本身不必知道 API 层怎么建引擎,
    也便于测试时换成假的执行体。
    """

    def __init__(
        self,
        schedule_file: Path | None = None,
        holiday_file: Path | None = None,
    ) -> None:
        self._schedule_file = schedule_file or DEFAULT_SCHEDULE_FILE
        self._states: dict[str, ScheduleState] = {}
        self._scheduler: Any = None
        self._runner: Callable[[str, int], dict[str, Any]] | None = None
        self._lock = threading.RLock()
        self._holidays = load_holidays(holiday_file)
        self._restore_error = ""

    # ==================== 执行体 ====================

    def set_runner(self, runner: Callable[[str, int], dict[str, Any]] | None) -> None:
        """注入执行体: ``runner(account_id, days) -> {"status": ..., "persisted": bool}``"""
        self._runner = runner

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
        if state.consecutive_failures >= 2:
            warns.append(f"已连续失败 {state.consecutive_failures} 次, 请查看 last_error")
        return warns

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
            return {"status": "ok", "account_id": account_id, **state.last_result}

    def _record_failure(
        self,
        state: ScheduleState,
        now: datetime,
        exc: Exception,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """记录失败 —— 日志 + 可查询的 status 双写, 不允许静默"""
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
        return {"status": "error", "account_id": state.account_id, "error": state.last_error}

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
