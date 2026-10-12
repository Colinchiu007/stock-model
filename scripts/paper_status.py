"""模拟盘观察期状态一览 —— 观察期(≥2-4 周)每天一条命令查看

用法:
    python scripts/paper_status.py            # 本机服务(8123)
    python scripts/paper_status.py 8124      # 指定端口

输出内容与判读:
  - 服务是否活着(死了会提示用兜底脚本拉起)
  - 调度状态: 上次结果 / 连续失败 / 距上次活动多久 / 下次执行
  - 账户: 总资产 / 现金 / 成交笔数 / 交易天数
  - 观察期目标: ≥20 笔成交前不评估策略(HANDOVER 第三节)
退出码: 0 = 服务可达; 1 = 不可达
"""

from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
TZ = ZoneInfo("Asia/Shanghai")


def get(port: int, path: str, timeout: int = 10):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _trades_count(port: int) -> int:
    """成交笔数 —— **必须**从 /api/paper/trades 拿

    /api/paper/account 没有 trades/total_trades 字段(只有 cash/total_asset/
    trading_days 等) —— 曾按臆测字段读, 导致实际 2 笔成交却显示"成交 0 笔"
    (2026-10-13 实测 bug)。trades 接口失败时降级为 0(观察工具不因单接口挂而崩)。
    """
    try:
        data = get(port, "/api/paper/trades?account_id=default", timeout=60)
    except Exception:
        return 0
    trades = data.get("trades")
    return len(trades) if isinstance(trades, list) else int(data.get("total") or 0)


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8123
    try:
        get(port, "/api/health")
    except Exception as e:
        print(f"✗ 服务不可达 (127.0.0.1:{port}): {e}")
        print("  → 服务没起? 跑: python scripts/paper_daily_fallback.py")
        return 1

    sched = get(port, "/api/paper/schedule?account_id=default")
    try:
        # 空账户/首跑时该接口可能触发数据初始化, 会慢(实测 >10s) —— 给足超时
        acc = get(port, "/api/paper/account?account_id=default", timeout=180)
    except Exception as e:
        print(f"⚠️ 账户接口暂不可用({type(e).__name__}), 调度状态仍有效:")
        print("   (可能正在初始化引擎/拉数据 —— 稍后重跑本脚本)")
        acc = None

    now = datetime.now(TZ)
    print(f"═══ 模拟盘观察  {now.strftime('%Y-%m-%d %H:%M')} ═══")

    # 调度
    status_emoji = {"ok": "✅", "error": "❌", "skipped": "⏭️", "": "·"}.get(
        sched.get("last_status", ""), "·"
    )
    print(f"定时: {'已开启' if sched.get('running') else '未开启'}"
          f" | 下次: {sched.get('next_run_time') or '—'}")
    print(f"上次: {status_emoji} {sched.get('last_status') or '无'}"
          f" @ {sched.get('last_run_at') or '—'}")
    if sched.get("last_status") == "error":
        print(f"   错误: {sched.get('last_error')}")
        if sched.get("consecutive_failures", 0) >= 2:
            print(f"   ⚠️ 已连续失败 {sched['consecutive_failures']} 次")
    print(f"统计: 成功 {sched.get('run_count', 0)} / 失败 {sched.get('error_count', 0)}"
          f" / 跳过 {sched.get('skipped_count', 0)}"
          f" | 告警已发 {sched.get('alert_count', 0)} 条"
          f" ({sched.get('alert_channel', '—')})")
    for w in sched.get("warnings", []):
        print(f"   ⚠️ {w}")

    # 账户(接口不可用时跳过)
    if acc is None:
        print("账户: (本次未取到 —— 见上方提示)")
        print("观察期: 未评估 —— 等接口可用后再看成交数")
        print("═══")
        return 0
    ret = acc.get("total_return", 0) * 100
    print(f"账户: 总资产 ¥{acc.get('total_asset', 0):,.2f} ({ret:+.2f}%)"
          f" | 现金 ¥{acc.get('cash', 0):,.2f}")
    trades = _trades_count(port)
    print(f"      成交 {trades} 笔"
          f" | 交易日 {acc.get('trading_days', 0)} 天")

    # 观察期标尺
    if trades < 20:
        print(f"观察期: {trades}/20 笔 —— 未达标前不评估策略(HANDOVER 第三节)")
    else:
        print("观察期: 成交数已达标, 可进行首次策略评估(见 HANDOVER 3.x)")
    print("═══")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
