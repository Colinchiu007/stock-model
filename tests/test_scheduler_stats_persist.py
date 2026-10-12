"""调度器运行统计持久化 —— 修复重启后兜底重复推进

缺陷(2026-10-13 实测):
  重启服务后 running=True 但 last_run_at='' / run_count=0 /
  heartbeat='' —— **运行统计不落盘, 重启即丢**(配置 jobs 落盘了, 统计没有)。

后果链:
  1. 兜底脚本读 last_run_at 判断"今天已处理" → 读到空 → 认为没跑过
  2. 兜底再推进一个交易日 → **重复推进**(同一交易日撮合两次的风险)
  3. 实测已造成: 生产账户被 E2E 污染 + 数据分裂(服务内存 1 笔 vs 磁盘 2 笔)

契约:
1. run_count/last_run_at/last_status 等运行统计必须落盘
2. 重启后统计恢复(heartbeat/上次结果不丢)
3. 旧格式配置(只有 jobs 没有统计)不崩, 统计从零开始
"""

from __future__ import annotations

import json

import pytest

from stock_model.paper.scheduler import PaperScheduler


@pytest.fixture
def sched(tmp_path):
    s = PaperScheduler(schedule_file=tmp_path / "schedule.json")
    yield s
    s.shutdown()


def _run_once(s: PaperScheduler, now):
    """走公开路径跑一次(注入 runner + 指定时间), 产生运行统计"""
    # 需要真实 apscheduler(建 job) —— 缺依赖的环境(CI 主测试 job 只装
    # .[dev,quant])跳过, 与 test_paper_scheduler.py 的模式一致。
    pytest.importorskip("apscheduler")
    s.set_runner(lambda account_id, days: {"status": "ok", "steps": 1, "trades": 1})
    s.start("default", hour=15, minute=30, persist=True)
    return s._job("default", now=now)


class TestStatsPersist:
    def test_run_stats_survive_restart(self, sched, tmp_path):
        """运行统计必须落盘并在重启后恢复"""
        pytest.importorskip("apscheduler")
        from datetime import datetime
        from zoneinfo import ZoneInfo

        now = datetime(2026, 10, 12, 15, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
        r = _run_once(sched, now)
        assert r["status"] == "ok"
        assert sched.status("default")["run_count"] == 1

        # 模拟重启: 新实例读同一文件
        s2 = PaperScheduler(schedule_file=tmp_path / "schedule.json")
        try:
            s2.restore()
            st = s2.status("default")
            assert st.get("run_count") == 1, f"重启后 run_count 应恢复, 实际 {st.get('run_count')}"
            assert st.get("last_run_at"), "重启后 last_run_at 应恢复"
            assert st.get("last_status") == "ok"
        finally:
            s2.shutdown()

    def test_failure_stats_survive_restart(self, sched, tmp_path):
        """失败统计(连续失败数)也要恢复 —— 告警节流依赖它"""
        pytest.importorskip("apscheduler")
        from datetime import datetime
        from zoneinfo import ZoneInfo

        now = datetime(2026, 10, 12, 15, 30, tzinfo=ZoneInfo("Asia/Shanghai"))

        def boom(account_id, days):
            raise RuntimeError("数据源炸了")

        sched.set_runner(boom)
        sched.start("default", hour=15, minute=30, persist=True)
        sched._job("default", now=now)
        sched._job("default", now=now)

        s2 = PaperScheduler(schedule_file=tmp_path / "schedule.json")
        try:
            s2.restore()
            st = s2.status("default")
            assert st.get("consecutive_failures") == 2
            assert st.get("error_count") == 2
            assert "数据源炸了" in (st.get("last_error") or "")
        finally:
            s2.shutdown()

    def test_config_without_stats_still_works(self, tmp_path):
        """旧格式配置(只有 jobs 没有 stats)不崩, 统计从零开始"""
        pytest.importorskip("apscheduler")  # restore→start 需要真实调度器
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
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        s = PaperScheduler(schedule_file=f)
        try:
            s.restore()
            st = s.status("default")
            assert st.get("run_count", 0) == 0
        finally:
            s.shutdown()
