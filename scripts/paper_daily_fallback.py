# -*- coding: utf-8 -*-
"""模拟盘「今日兜底」脚本 —— 给进程外定时任务(harness/计划任务)调用

它解决什么问题
--------------
内置的 APScheduler 只在**服务进程活着**时有效。服务没起、机器重启后没人拉起,
"每天自动跑"就静默失效 —— 而且不会有任何失败记录(这是心跳检测的盲区)。

这个脚本是**进程外兜底**: 每天由外部调度器调用一次, 做 4 件事, 全部**幂等**:
  1. 服务没起     → 拉起(后台), 等健康检查通过
  2. 今天已跑过   → 什么都不做, 退出 0
  3. 今天还没跑   → 调 POST /api/paper/schedule/run 推进 1 个交易日
  4. 今天非交易日 → 接口返回 skipped, 视为"已处理", 退出 0

为什么走 HTTP 而不是直接 import 引擎
------------------------------------
单 worker 是硬约束(_assert_single_worker 会拒绝多 worker)。
若脚本直接跑引擎, 与常驻服务就是两个进程、两份内存状态,
可能对同一笔挂单撮合两次。走 HTTP 让兜底与界面/定时任务共用**同一个**进程状态。

怎么判定"今天已跑过"
--------------------
读 /api/paper/schedule 的 last_run_at(本地时区 ISO 字符串),
取其日期部分与今天比对。成功/**失败**/**跳过**都算"已处理" ——
失败已由告警通道负责提醒, 兜底不需要(也不应该)再推进一次去"重试",
因为失败多因数据源问题, 立即重试多半还是失败, 还可能与内置调度器撞车。

退出码: 0 = 无需处理或处理成功; 1 = 有异常(调度器应记录/通知)
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
# 不用 8000 这类常见默认端口: 实测本机 8000 被另一个常驻应用(everos)占用,
# 那种情况下脚本会在"health 不是本项目" -> "尝试再起一个"之间空转。
# 8123 是本项目重启 E2E(experiments/verify_paper_restart.ps1)的既有约定端口。
DEFAULT_PORT = 8123
PORT = DEFAULT_PORT
BASE = f"http://127.0.0.1:{PORT}"
LOG = Path(__file__).with_suffix(".log")
TZ = ZoneInfo("Asia/Shanghai")
WAIT_HEALTH_SECONDS = 60


def extract_port(args: list[str]) -> int:
    """从命令行解析 --port; 缺值/非数字都回退默认端口

    兜底脚本由外部调度器**无人值守**调用 —— 参数配错时宁可按默认端口继续,
    也不能抛 IndexError 让当天的账没人补。(misconfiguration ≠ 不处理)
    """
    if "--port" in args:
        i = args.index("--port")
        if i + 1 < len(args):
            try:
                return int(args[i + 1])
            except ValueError:
                print(f"⚠️ --port {args[i + 1]!r} 不是数字, 使用默认端口 {DEFAULT_PORT}")
    return DEFAULT_PORT


def today_handled(last_run_at: str, now: datetime, last_status: str = "") -> bool:
    """今天是否已处理(成功/失败/跳过都算)

    只比对**日期部分**, 且对格式异常宽容(返回 False 走补跑, 而不是抛异常):
    - 失败也算: 失败已由告警通道提醒; 兜底若再重试, 立即重试多半还是失败
      (数据源问题), 还可能与内置调度器撞车。
    - 未来时间戳(时钟回拨/别的机器写的)日期是今天就算已处理, 不重复推进。
    """
    if not last_run_at:
        return False
    today = now.date().isoformat()
    return last_run_at[:10] == today


def run_exit_code(result: dict) -> int:
    """执行结果 → 退出码; 未知状态宁可当失败(退出码是外部调度器唯一信号源)"""
    return 0 if result.get("status") in ("ok", "skipped") else 1


def http_json(path: str, method: str = "GET", payload: dict | None = None,
              timeout: int = 30) -> dict:
    req = urllib.request.Request(  # noqa: S310 - 固定本机地址
        f"{BASE}{path}", method=method,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        return json.loads(r.read().decode("utf-8"))


def server_alive() -> bool:
    try:
        return http_json("/api/health", timeout=3).get("status") == "ok"
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def start_server() -> subprocess.Popen | None:
    """后台拉起服务; 返回进程句柄(交给调用方决定是否留着)"""
    python = sys.executable
    log = open(LOG, "ab")  # noqa: SIM115 - 常驻日志, 交给服务进程持有
    return subprocess.Popen(  # noqa: S603 - 固定参数
        [python, "-m", "uvicorn", "--app-dir", "src",
         "stock_model.web.app:create_app", "--factory", "--port", str(PORT)],
        cwd=str(REPO), stdout=log, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def main() -> int:
    global PORT, BASE
    PORT = extract_port(sys.argv[1:])
    BASE = f"http://127.0.0.1:{PORT}"
    proc = None
    if not server_alive():
        print("服务未运行, 后台拉起…")
        proc = start_server()
        deadline = time.time() + WAIT_HEALTH_SECONDS
        while time.time() < deadline:
            if server_alive():
                break
            if proc.poll() is not None:
                print(f"✗ 服务进程退出 (exit={proc.returncode}), 详见 {LOG}")
                return 1
            time.sleep(1)
        else:
            print(f"✗ {WAIT_HEALTH_SECONDS}s 内健康检查未通过, 详见 {LOG}")
            return 1
        print("✓ 服务已就绪")
    else:
        print("✓ 服务已在运行")

    sched = http_json("/api/paper/schedule?account_id=default")
    now = datetime.now(TZ)
    last_run_at = sched.get("last_run_at") or ""
    already = today_handled(last_run_at, now, sched.get("last_status") or "")
    print(f"今日({now.date().isoformat()})已处理: {already}  "
          f"last_run_at={last_run_at!r}  last_status={sched.get('last_status')!r}")

    if already:
        print("无需处理, 退出")
        return 0

    # 先确保调度配置存在(接口幂等: 已开启则原样返回当前状态)。
    # 兜底可能在"服务被重新拉起、还没有任何配置"时运行 ——
    # 实测: 那时 run 会返回 error「账户 default 没有调度配置」, 兜底就白跑了。
    if not sched.get("running"):
        sched = http_json("/api/paper/schedule", method="POST",
                          payload={"account_id": "default", "hour": 15,
                                   "minute": 30, "days": 1})
        print(f"调度配置已就绪: running={sched.get('running')}")

    result = http_json("/api/paper/schedule/run", method="POST",
                       payload={"account_id": "default"}, timeout=900)
    status = result.get("status")
    print(f"执行结果: status={status} "
          f"steps={result.get('steps')} trades={result.get('trades')} "
          f"reason={result.get('reason') or result.get('error') or ''}")
    # skipped(非交易日) 与 ok 都算"今天的账已处理"; error/未知才算失败
    return run_exit_code(result)


if __name__ == "__main__":
    raise SystemExit(main())
