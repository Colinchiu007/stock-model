"""start() 不得抹掉已有运行统计 —— 重启后首次 start 的覆盖 bug

缺陷(2026-10-13 01:15 实测):
  重启服务后首次 POST /api/paper/schedule(或 restore 后又 start),
  start() 里 `state = self._states.get(id) or ScheduleState(...)` 新建
  全 0 状态 → _save_config() 把磁盘上的历史统计(run_count/last_run_at)
  **覆盖成 0** → 兜底的"今天已处理"判断再次失效。

契约:
1. start() 时若磁盘已有该账户的统计, 必须继承(不归零)
2. 新账户(磁盘无记录)仍从零开始
"""

from __future__ import annotations

import pytest

from stock_model.paper.scheduler import PaperScheduler


@pytest.fixture
def sched(tmp_path):
    s = PaperScheduler(schedule_file=tmp_path / "schedule.json")
    yield s
    s.shutdown()


def test_start_inherits_existing_stats(tmp_path):
    """start() 必须继承磁盘上已有的运行统计(不归零)"""
    f = tmp_path / "schedule.json"
    f.write_text(
        __import__("json").dumps(
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
                        "error_count": 1,
                        "skipped_count": 2,
                        "last_run_at": "2026-10-12T15:30:04",
                        "last_status": "ok",
                        "last_result": {"status": "ok", "steps": 1},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    s = PaperScheduler(schedule_file=f)
    try:
        pytest.importorskip("apscheduler")
        s.start("default", hour=16, minute=0, persist=True)
        st = s.status("default")
        assert st["run_count"] == 5, f"start 后 run_count 应继承 5, 实际 {st['run_count']}"
        assert st["last_run_at"] == "2026-10-12T15:30:04"
        assert st["last_status"] == "ok"
        # 但 trigger 配置应按新参数更新
        assert st["hour"] == 16
    finally:
        s.shutdown()


def test_start_new_account_starts_from_zero(tmp_path):
    """新账户(磁盘无记录)从零开始(不误继承别的账户)"""
    pytest.importorskip("apscheduler")
    s = PaperScheduler(schedule_file=tmp_path / "schedule.json")
    try:
        s.start("brand_new", hour=15, minute=30, persist=True)
        st = s.status("brand_new")
        assert st["run_count"] == 0
        assert st["last_run_at"] == ""
    finally:
        s.shutdown()
