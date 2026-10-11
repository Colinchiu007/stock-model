"""APScheduler 真实时钟触发验证 —— 补"cron 首次真实触发"的验证缺口

背景
----
模拟盘首个真实交易日的前夜(2026-10-11)做 pre-flight, 发现一个从未真实
发生过的组合: APScheduler 的 CronTrigger 在**真实时钟**下的触发。
至今所有验证都是 run_now(now=...) 手动驱动或 HTTP 触发 —— 部件都测过,
但"看起来会跑"与"真的会被时钟触发"之间的缝隙, 正是本项目假门禁类
缺陷(装了不跑/界面显示运行中但没跑)的藏身处。

方法(不碰运行中的服务, 不改系统时钟)
------------------------------------
独立进程里起一个真实 BackgroundScheduler + 真实 CronTrigger,
注册一个"下一分钟"的任务(用假 runner 计数), **真实等待时钟走到**:
  1. 每分钟触发(cron="* * * * *") → 60~70s 内应触发 1 次
  2. next_run_time 由 APScheduler 自己计算(不是我们传的)

判定: 计数器 ≥1 = 时钟触发链路真实工作。

产出: 打印 PASS/FAIL, 退出码 0/1。跑一次约 75 秒。
"""

from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")


def main() -> int:
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError as e:
        print(f"✗ 缺依赖: {e}")
        return 1

    calls: list[str] = []

    def fake_job() -> None:
        calls.append(datetime.now(TZ).isoformat())

    sched = BackgroundScheduler(timezone=TZ)
    # 真实 CronTrigger: 每分钟的第 0 秒触发(与生产 daily 15:30 用的是同一组件)
    trigger = CronTrigger(minute="*", timezone=TZ)
    sched.add_job(fake_job, trigger=trigger, id="clock_probe", max_instances=1,
                  coalesce=True, misfire_grace_time=30)
    sched.start()
    print(f"✓ 调度器已启动, 注册每分钟 cron 任务 @ "
          f"{datetime.now(TZ).strftime('%H:%M:%S')} —— 真实等待时钟触发…")

    # 等待最多 75 秒(cron 下一分钟点 + 余量)
    deadline = time.time() + 75
    while time.time() < deadline:
        time.sleep(2)
        if calls:
            break
    job = sched.get_job("clock_probe")
    nxt = job.next_run_time.isoformat() if job and job.next_run_time else "(无)"
    sched.shutdown(wait=False)

    if calls:
        print(f"✓✓ 真实时钟触发成功: {len(calls)} 次, 首次 {calls[0]}")
        print(f"   next_run_time(调度器自算): {nxt}")
        print("   → APScheduler 的 CronTrigger 在真实时钟下会被触发, "
              "明天的 15:30 走的是同一条链路")
        return 0
    print("✗ 75 秒内 cron 未触发 —— APScheduler 时钟触发链路有问题, "
          "明天的 15:30 不会跑! 请检查 apscheduler 版本/时区配置")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
