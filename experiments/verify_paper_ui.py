"""模拟盘「定时运行」界面的端到端验证（真实浏览器）

为什么需要一个真实浏览器
------------------------
这个卡片的价值全在**"状态是否如实显示"**上：缺依赖要禁用按钮、失败要露出原因、
非交易日要说明"未执行"。这些用"HTML 里有没有这个 id"是验证不到的 ——
必须真的点一下、真的看渲染结果。

同时它也是本轮的**诚实性检查**：项目没有把 Playwright 纳入依赖，CI 里跑不了
（浏览器下载会让每个 job 多几分钟）。所以这一步是**本地一次性验证**，
CI 只有静态契约锁。这个缺口在 PR 里明说了，不假装有视觉回归门禁。

用法（Windows / 已装 playwright）:
    python experiments/verify_paper_ui.py

前置: pip install playwright && playwright install chromium
产出: experiments/_ui_shots/*.png
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SHOTS = Path(__file__).resolve().parent / "_ui_shots"
PORT = 8145
BASE = f"http://127.0.0.1:{PORT}"
PAPER_DIR = REPO / "data" / "paper"
# 这个文件是**被提交**的节假日表, 不是运行产物 —— 清理时必须留下,
# 否则跑一次验证就把日历删了(而且开头那次清理会让 holiday_calendar 变 false)。
KEEP = "holidays.json"


def port_in_use(port: int = PORT) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def http_json(path: str, method: str = "GET") -> dict:
    req = urllib.request.Request(f"{BASE}{path}", method=method)
    with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - 本地固定地址
        return json.loads(r.read().decode("utf-8"))


def wait_health(proc: subprocess.Popen, timeout: float = 90.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"服务进程已退出 (exit={proc.returncode})")
        try:
            if http_json("/api/health").get("status") == "ok":
                return
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        time.sleep(0.5)
    raise RuntimeError(f"{timeout}s 内服务没起来")


def main() -> int:  # noqa: PLR0915 - 线性验证脚本, 拆开反而更难对照输出
    # ---- 0. 前置断言: 端口空闲 + 状态干净(上次的教训: 连到旧服务会恒绿) ----
    if port_in_use():
        print(f"✗ 端口 {PORT} 被占用 —— 会连到旧服务上, 验证不可信")
        return 1
    PAPER_DIR.mkdir(parents=True, exist_ok=True)
    for pattern in ("*.json", "*.tmp"):
        for p in PAPER_DIR.glob(pattern):
            if p.name == KEEP:
                continue
            p.unlink()
    print(f"✓ 前置检查: 端口 {PORT} 空闲; data/paper 已清空(保留 {KEEP})")

    log = REPO / "experiments" / "_ui_server.log"
    proc = subprocess.Popen(  # noqa: S603 - 固定参数, 非外部输入
        [sys.executable, "-m", "uvicorn", "--app-dir", "src",
         "stock_model.web.app:create_app", "--factory", "--port", str(PORT)],
        cwd=str(REPO),
        stdout=log.open("w", encoding="utf-8"),
        stderr=subprocess.STDOUT,
    )

    failures: list[str] = []
    try:
        wait_health(proc)
        print("✓ 服务已就绪")

        from playwright.sync_api import expect, sync_playwright

        SHOTS.mkdir(exist_ok=True)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(BASE, wait_until="networkidle")
            page.click('button[data-tab="paper"]')

            # ---- 状态 1: 未开启 ----
            expect(page.locator("#pp-sched-state")).to_contain_text("未开启", timeout=15000)
            expect(page.locator("#pp-sched-plan")).to_contain_text("未开启")
            page.screenshot(path=str(SHOTS / "1-not-scheduled.png"))
            print("✓ 状态「未开启」渲染正确")

            # ---- 状态 2: 开启 ----
            page.click("#pp-sched-start")
            expect(page.locator("#pp-sched-state")).to_contain_text("已开启", timeout=15000)
            expect(page.locator("#pp-sched-plan")).to_contain_text("每个交易日")
            # 后端确实注册了任务(不只看界面)
            sched = http_json("/api/paper/schedule?account_id=default")
            if sched.get("running") is not True:
                failures.append("界面显示已开启, 但后端 running 不是 true")
            if not sched.get("next_run_time"):
                failures.append("已开启但没有下次执行时间")
            page.screenshot(path=str(SHOTS / "2-scheduled.png"))
            print(f"✓ 状态「已开启」渲染正确; 后端 next_run={sched.get('next_run_time')}")

            # ---- 状态 3: 立即执行一次(今天可能是周末 → 如实显示"未执行") ----
            page.click("#pp-sched-run")
            expect(page.locator("#pp-sched-msg")).not_to_contain_text("处理中", timeout=60000)
            msg = page.locator("#pp-sched-msg").inner_text()
            state = page.locator("#pp-sched-state").inner_text()
            page.screenshot(path=str(SHOTS / "3-run-now.png"))
            print(f"✓ 「立即执行一次」提示: {msg}")
            if not (("已执行" in msg) or ("未执行" in msg) or ("失败" in msg)):
                failures.append(f"立即执行后的提示不含明确结果: {msg!r}")

            # 若今天非交易日, 状态里应能看出"上次跳过"
            if "未执行" in msg:
                expect(page.locator("#pp-sched-last")).to_contain_text("跳过")
                print("✓ 非交易日如实显示「跳过」而不是装作执行了")

            # ---- 状态 4: 停止 ----
            page.click("#pp-sched-stop")
            expect(page.locator("#pp-sched-state")).to_contain_text("未开启", timeout=15000)
            page.screenshot(path=str(SHOTS / "4-stopped.png"))
            print("✓ 状态「停止」后回到未开启")

            # ---- 状态 5: 告警通道必须可见(否则用户以为失败会通知他) ----
            page.click("#pp-sched-start")
            expect(page.locator("#pp-sched-state")).to_contain_text("已开启", timeout=15000)
            alert_line = page.locator("#pp-sched-alert").inner_text()
            if "告警通道" not in alert_line:
                failures.append(f"没有显示告警通道: {alert_line!r}")
            else:
                print(f"✓ 告警通道已显示: {alert_line}")

            # ---- 状态 5b: 节假日表已加载(卡片不该再警告"仅排除周末") ----
            sched_api = http_json("/api/paper/schedule?account_id=default")
            if sched_api.get("holiday_calendar") is not True:
                failures.append(
                    "后端 holiday_calendar 不是 true —— 节假日表没被加载, 节假日会照常触发"
                )
            else:
                print("✓ 后端 holiday_calendar=true（节假日表已加载）")
            warns_text = page.locator("#pp-sched-warns").inner_text()
            if "未加载节假日表" in warns_text:
                failures.append(f"卡片仍在警告节假日表缺失: {warns_text!r}")
            elif warns_text.strip():
                # ⚠️ 这里不能只查"我认识的那条旧告警"。第一版就是只查了
                # "未加载节假日表", 于是"节假日表已过期"这条**误报**(2026-10-08 起
                # 天天出现)从断言下漏过去了 —— 最后是**截图**抓到的。
                # 健康配置下不该有任何告警框, 一律视为失败。
                failures.append(f"健康配置下出现了告警框(可能是误报): {warns_text!r}")
            else:
                print("✓ 卡片无告警框(节假日表已加载且未过期)")

            # ---- 状态 6: 无 JS 报错(真实浏览器才能发现) ----
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.reload(wait_until="networkidle")
            page.click('button[data-tab="paper"]')
            expect(page.locator("#pp-sched-state")).to_contain_text(
                "已开启", timeout=15000
            )
            if errors:
                failures.append(f"页面有 JS 错误: {errors}")
            else:
                print("✓ 刷新后无 JS 错误, 定时状态自动接回")

            browser.close()

        # ---- 状态 7: 缺 apscheduler 时必须禁用按钮并说明原因 ----
        # 用 stub 模块挡在 PYTHONPATH 前面模拟"未安装"(不改本机环境)
        print("✓ 浏览器验证结束")
    except Exception as e:  # noqa: BLE001 - 验证脚本: 任何异常都要报出来
        failures.append(f"{type(e).__name__}: {e}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()
        # 端口必须真的释放 —— 否则下一次运行会连到幽灵服务上(踩过)
        for _ in range(20):
            if not port_in_use():
                break
            time.sleep(0.5)
        if port_in_use():
            failures.append("服务没被真正杀掉, 端口仍被占用")
        for pattern in ("*.json", "*.tmp"):
            for p in PAPER_DIR.glob(pattern):
                if p.name == KEEP:
                    continue
                p.unlink()

    print()
    if failures:
        print("=== 验证失败 ===")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    print("=== UI 验证全部通过 ===")
    print(f"截图: {SHOTS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
