"""趋势过滤器效果对照实验

回答: 加入趋势过滤后, 「上涨市跑输基准」是否改善?

做法: 同一动态股票池、同一组市场环境, 分别用
  - ManualStrategy(use_trend_filter=False)  原始行为
  - ManualStrategy(use_trend_filter=True)   启用趋势过滤
对比超额收益, 以及跌市表现是否受损(不应受损)。

判据:
  上涨市超额改善 且 下跌市超额未明显下降 → 过滤器有效
  上涨市无改善或跌市大幅下降           → 过滤代价大于收益

用法: python experiments/run_trend_filter_test.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from loguru import logger

logger.remove()
logger.add(sys.stderr, level="WARNING")

from stock_model.paper.engine import PaperEngine  # noqa: E402
from stock_model.paper.universe import UniverseConfig, UniverseSelector  # noqa: E402
from stock_model.strategy.manual import ManualStrategy  # noqa: E402

BENCHMARK = "000002"
POOL_SIZE = 8
AS_OF = "20260930"

SCENARIOS = [
    ("上涨市 2019", "20190101", "20191231"),
    ("横盘市 2020", "20200101", "20201231"),
    ("下跌市 2021", "20210101", "20211231"),
    ("横盘市 2022", "20220101", "20221231"),
    ("下跌市 2023", "20230101", "20231231"),
    ("下跌市 2024", "20240101", "20241231"),
]


def run(symbols, start, end, use_filter: bool):
    e = PaperEngine(
        symbols=symbols,
        initial_capital=10000.0,
        data_source="baostock",
        start_date=start,
        end_date=end,
        min_data_rows=60,
    )
    e.add_strategy(ManualStrategy(use_trend_filter=use_filter))
    steps = e.run()
    if not steps:
        return None
    rep = e.report(benchmark_symbol=BENCHMARK)
    m = rep["metrics"]
    if not rep.get("benchmark_available"):
        print(f"  ⚠️ {start}~{end} 基准无数据，超额不可信", flush=True)
        return None
    return {
        "trades": len(e.account.trades),
        "n": len(e.symbols),
        "strategy": m["total_return"],
        "benchmark": m["benchmark_return"],
        "alpha": m["alpha"],
    }


def main() -> None:
    print("=" * 96)
    print("趋势过滤器效果对照实验 · 动态股票池")
    print("=" * 96)

    cfg = UniverseConfig(top_n=POOL_SIZE, as_of=AS_OF, pre_screen_top=40, min_amount=5e7)
    pool = [c.symbol for c in UniverseSelector(cfg).build()]
    if not pool:
        print("股票池构建失败")
        return
    print(f"\n动态池({len(pool)}只): {pool}\n")
    print(f"{'场景':<12}{'原策略超额':>13}{'加过滤超额':>13}{'差异':>10}{'成交变化':>12}")
    print("-" * 72)

    ups, downs = [], []
    for name, start, end in SCENARIOS:
        base = run(pool, start, end, use_filter=False)
        filt = run(pool, start, end, use_filter=True)
        if not base or not filt:
            continue
        delta = (filt["alpha"] - base["alpha"]) * 100
        print(
            f"{name:<12}{base['alpha']:>12.2%}{filt['alpha']:>13.2%}"
            f"{delta:>9.2f}pp{base['trades']:>6}→{filt['trades']:<5}"
        )
        (ups if "上涨" in name else downs if "下跌" in name else []).append(
            (base["alpha"], filt["alpha"])
        )

    print()
    print("=" * 96)
    print("结论")
    print("=" * 96)
    if ups:
        b, f = ups[0]
        print(f"上涨市超额: {b:+.2%} -> {f:+.2%}  ({(f - b) * 100:+.2f}pp)")
        if f > b + 0.03:
            print("→ 上涨市显著改善, 趋势过滤有效")
        elif f > b:
            print("→ 上涨市略有改善, 过滤有效但幅度有限")
        else:
            print("→ 上涨市无改善, 根因不只是逆势卖出")
    if downs:
        b = sum(x[0] for x in downs) / len(downs)
        f = sum(x[1] for x in downs) / len(downs)
        print(f"下跌市平均超额: {b:+.2%} -> {f:+.2%}  ({(f - b) * 100:+.2f}pp)")
        if f < b - 0.05:
            print("⚠️ 跌市防御能力受损, 需复核过滤规则")


if __name__ == "__main__":
    main()
