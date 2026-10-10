# -*- coding: utf-8 -*-
"""死信开关（watchdog ping）—— 机器彻底关机/断网时的最后一块保障

为什么需要
----------
前面四层保障都跑在**这台机器上**：机器彻底关机/断网时，兜底脚本也跑不了，
心跳只在「有人来查」时提示 —— 没人查就永远没人知道。

死信开关的思路（业界标准做法，healthchecks.io 首创）：
外部监控服务提供一个 URL，你的任务**每次跑完都 ping 它**；
若监控服务在预期周期内**没收到** ping，就**由它主动**发通知（邮件/短信/APP）。
通知能力在云端，不依赖这台机器活着 —— 这正是它补上的那块盲区。

用法（两步，缺一不可）
--------------------
1. 在 healthchecks.io（或自建）建一个 Check，拿到 ping URL
2. 写进 `.env`：``STOCK_PING_URL=https://hc-ping.com/xxxx``
   并把外部调度器（harness/计划任务）的**检查周期**配成 ≥ 兜底脚本的调度间隔
   （兜底每天跑一次 → Check 周期设 1~2 天，宽余量防误报）

谁负责 ping
-----------
``scripts/paper_daily_fallback.py`` 每次运行结束时 ping 一次（无论成败，
失败时 ping ``/fail`` 后缀让监控服务立即告警而不是等超时）。
选它而不是内置调度器：兜底是**进程外**的，服务死了它照样能跑、能 ping；
ping 挂在服务进程里就成了"自己给自己报平安"。

设计原则
--------
- ping 失败**绝不能**影响兜底本身的退出码（监控挂了不能让账没人补），
  但要打日志 —— 不静默
- 未配置 URL 时完全静默跳过（不打扰）
"""

from __future__ import annotations

import urllib.error
import urllib.request

PING_TIMEOUT = 10


def ping(url: str | None, *, ok: bool = True, timeout: int = PING_TIMEOUT) -> bool:
    """向死信开关发一次 ping

    Args:
        url: 监控服务的 ping URL；``None``/空 = 未配置，静默跳过
        ok: True → ping 正常端点；False → ping ``/fail``（立即告警而非等超时）

    Returns:
        是否成功送达（未配置返回 False —— 但调用方应区分"未配置"与"失败"，
        见 :func:`ping_quiet`）
    """
    if not url:
        return False
    target = url.rstrip("/") + ("" if ok else "/fail")
    try:
        req = urllib.request.Request(target, method="GET")  # noqa: S310 - 用户配置的 URL
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 300
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        print(f"⚠️ 死信开关 ping 失败({target}): {e} —— 兜底本身不受影响, "
              f"但这次运行监控服务看不到, 超时后它可能误报")
        return False


def ping_quiet(url: str | None, *, ok: bool = True) -> bool:
    """ping 的"未配置"感知封装：未配置返回 None，供调用方打一行提示"""
    if not url:
        print("ℹ️ 未配置死信开关(STOCK_PING_URL) —— 机器彻底关机时将无人通知")
        return None
    return ping(url, ok=ok)
