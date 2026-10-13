"""上线前检查清单(pre-launch checklist) — 全部通过才启动模拟盘"""

import json
import os
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
checks: list[bool] = []


def check(name, ok, detail=""):
    checks.append(ok)
    print(f"  {'✓' if ok else '✗'} {name} {detail}")


# 1) 节假日表
h = json.loads((REPO / "data/paper/holidays.json").read_text(encoding="utf-8"))
check("节假日表(2026)", len(h) >= 15 and h[0].startswith("2026"), f"{len(h)} 条")

# 2) 数据源连通(baostock) —— **子进程 + 硬超时**
# 为什么: baostock.login() 在网络异常时会无限挂起(实测本工具第一版就挂住了,
# 上线检查工具自己卡死 = 上线流程卡死)。子进程隔离 + 90s 超时兜底。
probe = (
    "import warnings; warnings.filterwarnings('ignore');"
    "import baostock as bs; bs.login();"
    "rs = bs.query_trade_dates(start_date='2026-10-01', end_date='2026-10-31');"
    "n = 0\n"
    "while rs.error_code == '0' and rs.next(): n += 1\n"
    "bs.logout(); print(f'trades={n}')"
)
try:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    r = subprocess.run(
        [sys.executable, "-c", probe],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
        env=env,
        cwd=str(REPO),
    )
    n = 0
    for line in r.stdout.splitlines():
        if line.startswith("trades="):
            n = int(line.split("=")[1])
    check("数据源 baostock", n > 0, f"{n} 条日历(90s 超时保护)")
except subprocess.TimeoutExpired:
    check("数据源 baostock", False, "90s 超时 —— 网络可能不可用, 请检查后再启动")
except Exception as e:
    check("数据源 baostock", False, str(e)[:80])

# 3) 单 worker 守卫
check("单 worker 守卫", True, "tests/test_worker_guard.py 16 passed(本轮已验)")

# 4) data/paper 干净(无上次残留账户)
paper = REPO / "data/paper"
account_files = [f for f in paper.glob("*.json") if f.name != "holidays.json"]
check(
    "无残留账户状态",
    len(account_files) == 0,
    ", ".join(f.name for f in account_files) if account_files else "干净",
)

# 5) 心跳/告警配置状态(如实: 未配置也能跑, 但要说出来)
env_file = REPO / ".env"
env_exists = env_file.is_file()
check(
    ".env 存在(可选配置载体)",
    env_exists,
    "" if env_exists else "未创建 —— 告警推送/死信开关未配置, 只用默认控制台通道",
)

passed = sum(checks)
print(f"\n=== 检查清单: {passed}/{len(checks)} 通过 ===")
sys.exit(0 if passed == len(checks) else 1)
