"""策略有效性对照实验

回答一个问题: **策略是"真有效", 还是"只是躲对了某一段行情"?**

做法: 在不同市场环境下分别跑模拟盘, 与买入持有基准对比。

判据:
  - 上涨市跑赢 -> 策略能跟上趋势, 有价值
  - 上涨市跑输 -> 策略是"抗跌工具", 只适合阴跌市
  - 各环境都跑赢 -> 才是真有效

用法: python experiments/run_regime_test.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stock_model.paper.engine import PaperEngine
from stock_model.strategy.manual import ManualStrategy

SYMBOLS = ["000002", "000001", "600036"]
BENCHMARK = "000002"

# (名称, 起始, 结束) —— 依据 000002 的真实年度涨跌划分
SCENARIOS = [
    ("上涨市 2019", "20190101", "20191231"),
    ("横盘市 2020", "20200101", "20201231"),
    ("下跌市 2021", "20210101", "20211231"),
    ("横盘市 2022", "20220101", "20221231"),
    ("下跌市 2023", "20230101", "20231231"),
    ("下跌市 2024", "20240101", "20241231"),
    ("全区间 2019-2026", "20190101", "20260930"),
]


def run(name: str, start: str, end: str) -> dict:
    """跑一个场景"""
    engine = PaperEngine(
        symbols=SYMBOLS,
        initial_capital=10000.0,
        data_source="baostock",
        start_date=start,
        end_date=end,
        min_data_rows=60,
    )
    engine.add_strategy(ManualStrategy())
    steps = engine.run()
    if not steps:
        return {"name": name, "error": "无数据"}

    rep = engine.report(benchmark_symbol=BENCHMARK)
    m = rep["metrics"]
    return {
        "name": name,
        "days": len(steps),
        "from": steps[0]["date"],
        "to": steps[-1]["date"],
        "trades": len(engine.account.trades),
        "trips": m["round_trips"],
        "strategy": m["total_return"],
        "benchmark": m["benchmark_return"],
        "alpha": m["alpha"],
        "mdd": m["max_drawdown"],
        "win_rate": m["win_rate"],
        "final": rep["account"]["total_asset"],
    }


def main() -> None:
    print("=" * 92)
    print("策略有效性对照实验 · 初始资金 1 万元 · 标的 000002/000001/600036")
    print("=" * 92)

    results = []
    for name, start, end in SCENARIOS:
        print(f"\n>>> 跑 {name} ({start}~{end}) ...", flush=True)
        r = run(name, start, end)
        results.append(r)
        if r.get("error"):
            print(f"    失败: {r['error']}")
            continue
        print(
            f"    {r['days']}天 {r['trades']}笔成交 | "
            f"策略{r['strategy']:+.2%} 基准{r['benchmark']:+.2%} "
            f"超额{r['alpha']:+.2%}"
        )

    # ---- 汇总 ----
    print("\n" + "=" * 92)
    print("汇总")
    print("=" * 92)
    print(
        f"{'场景':<20}{'天数':>5}{'成交':>6}{'策略':>10}{'基准':>10}"
        f"{'超额':>10}{'回撤':>9}{'胜率':>8}"
    )
    print("-" * 92)
    for r in results:
        if r.get("error"):
            continue
        print(
            f"{r['name']:<20}{r['days']:>5}{r['trades']:>6}"
            f"{r['strategy']:>9.2%}{r['benchmark']:>10.2%}"
            f"{r['alpha']:>10.2%}{r['mdd']:>9.2%}{r['win_rate']:>8.1%}"
        )

    up = [r for r in results if r.get("name", "").startswith("上涨")]
    down = [r for r in results if r.get("name", "").startswith("下跌")]
    if up:
        a = up[0]["alpha"]
        print()
        print("=" * 92)
        print("结论")
        print("=" * 92)
        if a > 0:
            print(f"上涨市跑赢基准 {a:+.2%} —— 策略能跟上趋势")
        else:
            print(f"❌ 上涨市跑输基准 {a:+.2%}")
            print("   策略在单边上涨中踏空, 属于「抗跌工具」而非趋势跟随策略。")
            print("   其超额收益来自「市场跌得更多」, 而非选股/择时能力。")
    if down:
        print(f"下跌市平均超额: {sum(r['alpha'] for r in down) / len(down):+.2%}")


if __name__ == "__main__":
    main()
