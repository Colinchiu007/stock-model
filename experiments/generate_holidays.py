"""生成 A 股节假日表（data/paper/holidays.json）

为什么需要这个脚本，而不是手写一份 JSON
----------------------------------------
把**真实交易日**误标成假日，代价是**漏掉真实交易日**（该跑的那天不跑），
比"没有日历"更糟 —— 因为后者至少每天都会跑一遍、由引擎兜底空转。
所以假日历必须由**权威数据推导**，不能凭印象手写。

做法：用本项目自己的**两个互相独立**的数据源求交易日集合，**逐日比对**：
  · baostock  `query_trade_dates`（本项目文档标注为"稳定备选"）
  · akshare   `tool_trade_date_hist_sina`（新浪的交易日历）

两者对同一年份的交易日集合**必须完全一致**，否则**拒绝写盘**并列出差异
（fail-closed：宁可没有日历，也不能写一份无法确认的日历）。
确认一致后，节假日 = 该年的工作日 − 交易日。

用法:
    python experiments/generate_holidays.py            # 默认生成"当前年"
    python experiments/generate_holidays.py 2026       # 指定年份

⚠️ 覆盖范围只到数据源为止。今日实测两个源都只到 2026-12-31，
**没有 2027** —— 所以不要扩展到未确认的年份。文件过期后
`scheduler` 会给出 warning（见 `PaperScheduler._warnings`），不会静默降级。
"""

from __future__ import annotations

import json
import sys
import warnings
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
OUTPUT = REPO / "data" / "paper" / "holidays.json"

# ⚠️ 刻意**不**在模块级调用 warnings.filterwarnings / 建 ZoneInfo:
# 本模块会被 tests/test_paper_scheduler.py 导入以锁定 cross_check 的 fail-closed 行为,
# 模块级副作用(全局关掉告警)会污染整个测试会话。副作用只放在 main() 里。


def trading_days_from_baostock(year: int) -> set[str]:
    """从 baostock 取该年交易日"""
    import baostock as bs

    bs.login()
    try:
        rs = bs.query_trade_dates(start_date=f"{year}-01-01", end_date=f"{year}-12-31")
        if rs.error_code != "0":
            raise RuntimeError(f"baostock 返回错误: {rs.error_code} {rs.error_msg}")
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
    finally:
        bs.logout()

    days = {r[0] for r in rows if r[1] == "1"}
    if not days:
        raise RuntimeError(f"baostock 没有返回 {year} 年任何交易日")
    return days


def trading_days_from_akshare(year: int) -> set[str]:
    """从 akshare(新浪) 取该年交易日"""
    import akshare as ak

    df = ak.tool_trade_date_hist_sina()
    days = {str(d) for d in df["trade_date"] if str(d).startswith(str(year))}
    if not days:
        raise RuntimeError(f"akshare 没有返回 {year} 年任何交易日(可能尚未发布)")
    return days


def cross_check(year: int, a: set[str], b: set[str]) -> None:
    """两个源必须逐日一致 —— 不一致就拒绝写盘(fail-closed)"""
    only_a = sorted(a - b)
    only_b = sorted(b - a)
    if only_a or only_b:
        raise RuntimeError(
            f"{year} 年两个数据源不一致, 拒绝生成假日历:\n"
            f"  仅 baostock 有: {only_a}\n"
            f"  仅 akshare  有: {only_b}\n"
            f"两个源不一致说明至少有一个不可靠, 此时写出的日历可能把真实交易日"
            f"误标为假日 —— 那会比「没有日历」更糟。请人工核对后再跑。"
        )


def weekdays_of(year: int) -> list[date]:
    day = date(year, 1, 1)
    out = []
    while day.year == year:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def blocks(days: list[date]) -> list[str]:
    """把连续日期合并成区间, 便于人工一眼核对(春节/国庆应该成块出现)"""
    if not days:
        return []
    out = []
    start = prev = days[0]
    for d in days[1:]:
        if (d - prev).days == 1:
            prev = d
            continue
        out.append(f"{start} ~ {prev}" if start != prev else f"{start}")
        start = prev = d
    out.append(f"{start} ~ {prev}" if start != prev else f"{start}")
    return out


def main() -> int:
    warnings.filterwarnings("ignore")  # akshare/baostock 的第三方告警与本次判断无关
    tz = ZoneInfo("Asia/Shanghai")
    year = int(sys.argv[1]) if len(sys.argv) > 1 else datetime.now(tz).year

    print(f"=== 生成 {year} 年 A 股节假日表 ===")
    bs_days = trading_days_from_baostock(year)
    ak_days = trading_days_from_akshare(year)
    print(f"  baostock: {len(bs_days)} 个交易日")
    print(f"  akshare : {len(ak_days)} 个交易日")

    cross_check(year, bs_days, ak_days)
    print("  ✓ 两个独立源逐日一致")

    trading = bs_days
    holiday_days = sorted(d for d in weekdays_of(year) if d.isoformat() not in trading)
    holidays = [d.isoformat() for d in holiday_days]

    # 合理性校验: 数量级不对就说明取数出了问题
    total_weekdays = len(weekdays_of(year))
    if not 10 <= len(holidays) <= 30:
        raise RuntimeError(
            f"{year} 年推导出 {len(holidays)} 个节假日, 数量不合理"
            f"(工作日 {total_weekdays}, 交易日 {len(trading)}) —— 取数可能有问题"
        )

    print(f"  工作日 {total_weekdays} − 交易日 {len(trading)} = 节假日 {len(holidays)}")
    print("  节假日区间(人工核对用, 春节/国庆应成块):")
    for b in blocks(holiday_days):
        print(f"    {b}")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    # newline="" —— 不做行尾转换: 否则 Windows 上写 CRLF、Linux 上写 LF,
    # 换个平台重新生成就会产生"内容没变但整文件 diff"的假改动。
    with open(OUTPUT, "w", encoding="utf-8", newline="") as f:
        f.write(json.dumps(holidays, ensure_ascii=False, indent=2) + "\n")
    print(f"\n已写入 {OUTPUT} ({len(holidays)} 条)")
    print(
        f"数据来源: baostock query_trade_dates + akshare tool_trade_date_hist_sina "
        f"(两者逐日一致)\n覆盖范围: {year} 全年 —— 跨年后本表失效, "
        f"scheduler 会给出 warning 提醒重新生成"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
