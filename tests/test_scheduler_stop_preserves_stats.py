"""停止定时不得抹掉累计统计 —— start/stop 路径的同族漏网

缺陷(2026-10-19 实测):
  手动 POST run 后为防当天重复推进, 调了一次 DELETE /api/paper/schedule,
  再 POST 恢复 —— 统计从「成功 3 / 失败 0 / 跳过 3」变成 **0/0/0**,
  `last_run_at` 也变空。

根因: ``stop()`` 先 ``self._states.pop(account_id)`` 再 ``_save_config()``,
而 ``_save_config()`` 只序列化 ``_states`` —— 弹掉就等于把该账户的
配置+统计从 schedule.json 里删掉。随后 ``start()`` 的
``_new_state_inheriting_stats()`` 在磁盘上找不到记录, 只能从 0 开始。

危害不只是"显示 0": ``last_run_at`` 是兜底脚本判断"今天已处理"的唯一依据,
被抹掉后兜底会在当天再推进一个交易日(重复撮合风险) —— 与 2026-10-13
那两次缺陷(统计不落盘 / start 抹历史)是同一族。

契约:
1. stop() 保留累计统计(停止 ≠ 清空历史)
2. stop() 后 schedule.json 里该账户标记 running=False
3. 进程重启后**不得**自动恢复已停止的任务
4. 已停止账户再次 start() 时继承统计
5. 旧格式(无 running 字段)按"在跑"处理, 保持向后兼容
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

import pytest

from stock_model.paper.scheduler import PaperScheduler

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def sched(tmp_path: Path):
    s = PaperScheduler(schedule_file=tmp_path / "schedule.json")
    yield s
    s.shutdown()


def _seed_stats(s: PaperScheduler, account_id: str = "default") -> None:
    """模拟"已经跑过几次"的累计统计"""
    st = s._states[account_id]
    st.run_count = 3
    st.skipped_count = 3
    st.error_count = 0
    st.last_run_at = "2026-10-18T19:00:28"
    st.last_status = "skipped"
    st.last_result = {"status": "skipped", "date": "2026-10-18"}


def _jobs_file(tmp_path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((tmp_path / "schedule.json").read_text(encoding="utf-8"))
    return cast("dict[str, Any]", data["jobs"])


class TestStopPreservesStats:
    def test_stop_keeps_stats_in_memory(self, tmp_path: Path):
        """停止再开启: 统计必须还在"""
        pytest.importorskip("apscheduler")
        s = PaperScheduler(schedule_file=tmp_path / "schedule.json")
        try:
            s.start("default", hour=15, minute=30)
            _seed_stats(s)
            s.stop("default")
            s.start("default", hour=15, minute=30)
            st = s.status("default")
            assert st["run_count"] == 3, f"停止后再开启 run_count 应仍为 3, 实际 {st['run_count']}"
            assert st["skipped_count"] == 3
            assert st["last_run_at"] == "2026-10-18T19:00:28"
            assert st["last_status"] == "skipped"
        finally:
            s.shutdown()

    def test_stop_marks_not_running_in_file(self, tmp_path: Path):
        """停止后落盘标记 running=False(而不是把记录删掉)"""
        pytest.importorskip("apscheduler")
        s = PaperScheduler(schedule_file=tmp_path / "schedule.json")
        try:
            s.start("default", hour=15, minute=30)
            _seed_stats(s)
            s.stop("default")
            jobs = _jobs_file(tmp_path)
            assert "default" in jobs, "停止后不应从 schedule.json 里删掉该账户"
            assert jobs["default"]["running"] is False
            assert jobs["default"]["run_count"] == 3, "统计必须保留在文件里"
        finally:
            s.shutdown()

    def test_restore_does_not_resume_stopped(self, tmp_path: Path):
        """进程重启后不得自动恢复已停止的任务"""
        pytest.importorskip("apscheduler")
        s1 = PaperScheduler(schedule_file=tmp_path / "schedule.json")
        try:
            s1.start("default", hour=15, minute=30)
            _seed_stats(s1)
            s1.stop("default")
        finally:
            s1.shutdown()

        s2 = PaperScheduler(schedule_file=tmp_path / "schedule.json")
        try:
            info = s2.restore()
            assert info.get("restored", 0) == 0, f"已停止的任务被误恢复: {info}"
            assert s2.status("default")["running"] is False
        finally:
            s2.shutdown()

    def test_stopped_stats_survive_restart_then_start(self, tmp_path: Path):
        """停止 -> 进程重启 -> 再开启: 统计仍能继承"""
        pytest.importorskip("apscheduler")
        s1 = PaperScheduler(schedule_file=tmp_path / "schedule.json")
        try:
            s1.start("default", hour=15, minute=30)
            _seed_stats(s1)
            s1.stop("default")
        finally:
            s1.shutdown()

        s2 = PaperScheduler(schedule_file=tmp_path / "schedule.json")
        try:
            s2.restore()
            s2.start("default", hour=15, minute=30)
            st = s2.status("default")
            assert st["run_count"] == 3, f"重启后再开启 run_count 应仍为 3, 实际 {st['run_count']}"
            assert st["last_run_at"] == "2026-10-18T19:00:28"
        finally:
            s2.shutdown()

    def test_running_task_is_restored(self, tmp_path: Path):
        """在跑的任务仍必须被恢复(不能因为加了标记就不恢复)"""
        pytest.importorskip("apscheduler")
        s1 = PaperScheduler(schedule_file=tmp_path / "schedule.json")
        try:
            s1.start("default", hour=15, minute=30)
        finally:
            s1.shutdown()

        s2 = PaperScheduler(schedule_file=tmp_path / "schedule.json")
        try:
            info = s2.restore()
            assert info.get("restored", 0) == 1, f"在跑的任务应被恢复: {info}"
            assert s2.status("default")["running"] is True
        finally:
            s2.shutdown()

    def test_legacy_file_without_running_key_resumes(self, tmp_path: Path):
        """旧格式(无 running 字段)按"在跑"处理, 保持向后兼容"""
        pytest.importorskip("apscheduler")
        f = tmp_path / "schedule.json"
        f.write_text(
            json.dumps(
                {
                    "version": 1,
                    "jobs": {
                        "default": {
                            "trigger_type": "daily",
                            "hour": 15,
                            "minute": 30,
                            "days": 1,
                            "timezone": "Asia/Shanghai",
                            "started_at": "2026-10-12T15:30:00",
                            "run_count": 5,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        s = PaperScheduler(schedule_file=f)
        try:
            info = s.restore()
            assert info.get("restored", 0) == 1, f"旧格式应照常恢复: {info}"
            assert s.status("default")["run_count"] == 5
        finally:
            s.shutdown()
