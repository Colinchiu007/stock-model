#!/usr/bin/env python
"""把模拟盘账户迁移到「滚动窗口」口径(方案 A, A1 重置)

用法:
    python scripts/migrate_rolling_window.py [--account default] [--dry-run]

背景(2026-10-21 实测):
    原先 start_date=20240101 写死, 账户每交易日爬一根 bar。今天 cursor
    才到 2024-04-17, 比"今天"慢 2.4 年 —— 观察期攒 20 笔成交要等一两年,
    且评估的是 2024 年的市场, 对当下没参考价值。

本脚本做的事(口径由用户选定为 A1 重置):
    1. start_date -> 滚动窗口起点(最近 DEFAULT_LOOKBACK_BARS 根)
    2. cursor     -> 0(从窗口第 0 根开跑)
    3. 现金回初始资金; 清空成交/持仓/资金曲线
    4. 保留 symbols / data_source / initial_capital(用户配置)
    5. 迁移前备份到 *.bak_migrate_<时间戳>(可回滚)

安全性:
    - 默认 dry-run, 只打印将要发生什么, 不改文件
    - 加 --yes 才真正执行
    - 函数幂等: 已迁移过的账户再跑一次不会二次破坏
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))


def main() -> int:
    ap = argparse.ArgumentParser(description="迁移模拟盘账户到滚动窗口口径")
    ap.add_argument("--account", default="default", help="账户 ID(默认 default)")
    ap.add_argument("--yes", action="store_true", help="真正执行(默认只预演)")
    args = ap.parse_args()

    from stock_model.web.paper_api import (
        DEFAULT_LOOKBACK_BARS,
        migrate_to_rolling_window,
    )

    path = REPO / "data" / "paper" / f"{args.account}.json"
    if not path.exists():
        print(f"账户文件不存在: {path}")
        return 1

    before = json.loads(path.read_text(encoding="utf-8"))
    meta = before.get("_metadata", {})
    old_start = meta.get("start_date")
    cursor_vals = list((meta.get("cursor") or {}).values())
    old_cursor: object = cursor_vals[0] if cursor_vals else 0

    print("=== 迁移前 ===")
    print(f"  账户        : {args.account}")
    print(f"  start_date  : {old_start}")
    print(f"  cursor      : {old_cursor}")
    print(
        f"  成交/持仓   : {len(before.get('trades') or [])} 笔 / "
        f"{list(before.get('positions') or {})}"
    )
    print(f"  资金曲线    : {len(before.get('equity_curve') or [])} 点")
    print(f"  现金        : {float(before.get('cash') or 0):.2f}")
    print(f"  保留        : symbols={meta.get('symbols')} data_source={meta.get('data_source')}")

    if not args.yes:
        print(
            f"\n[预演] 将把 start_date 改为滚动窗口起点"
            f"(最近 {DEFAULT_LOOKBACK_BARS} 根), cursor 归 0,"
        )
        print("       现金回初始资金, 并清空成交/持仓/资金曲线(迁移前自动备份)。")
        print("       确认无误后加 --yes 执行。")
        return 0

    info = migrate_to_rolling_window(path)
    print("\n=== 迁移完成 ===")
    print(f"  备份        : {Path(info['backup']).name}")
    print(f"  start_date  : {info['start_date']}")
    print(f"  cursor      : {info['cursor']}")
    print(f"  现金        : {info['initial_capital']:.2f}")
    print(
        "\n下一步: 重启服务让新起点生效"
        "  (python -m uvicorn --app-dir src stock_model.web.app:create_app --factory --port 8123)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
