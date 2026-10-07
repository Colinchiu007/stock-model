"""动态股票池 vs 伪分散池 —— 对照实验

回答: 之前评估报告里「上涨市跑输 39%」的结论,
是**策略本身的方向问题**, 还是**股票池伪分散导致的假象**?

做法: 用动态筛选出的分散股票池重跑同一组市场环境,
与原来的银行地产池(伪分散)对比超额收益。

若动态池下上涨市仍大幅跑输 → 确认是策略方向问题
若上涨市表现改善     → 原结论部分来自池子缺陷, 需修正评估报告

用法: python experiments/run_dynamic_pool_test.py
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

# 原评估报告使用的伪分散池（银行+地产）
OLD_POOL = ["000002", "000001", "600036"]

BENCHMARK = "000002"

SCENARIOS = [
    ("上涨市 2019", "20190101", "20191231"),
    ("横盘市 2020", "20200101", "20201231"),
    ("下跌市 2021", "20210101", "20211231"),
    ("横盘市 2022", "20220101", "20221231"),
    ("下跌市 2023", "20230101", "20231231"),
    ("下跌市 2024", "20240101", "20241231"),
]


def build_dynamic_pool(size: int = 8, as_of: str = "20260930") -> list[str]:
    """构建动态股票池

    ⚠️ 关键: 必须用 as_of=20260930 构建, 保证在回测各区间"当时"
    只使用当时可见的信息。若用今天的池子回测 2019 年,
    就引入了未来函数(幸存者偏差)。
    """
    print(f"构建动态股票池(as_of={as_of}, size={size})...", flush=True)
    cfg = UniverseConfig(
        top_n=size,
        as_of=as_of,
        pre_screen_top=max(40, size * 4),
        min_amount=5e7,
    )
    cands = UniverseSelector(cfg).build()
    pool = [c.symbol for c in cands]
    print(f"  -> {len(pool)} 只: {pool}")
    return pool


def run_pool(symbols: list[str], start: str, end: str) -> dict | None:
    """在给定股票池上跑一个市场环境"""
    e = PaperEngine(
        symbols=symbols,
        initial_capital=10000.0,
        data_source="baostock",
        start_date=start,
        end_date=end,
        min_data_rows=60,
    )
    e.add_strategy(ManualStrategy())
    steps = e.run()
    if not steps:
        return None
    rep = e.report(benchmark_symbol=BENCHMARK)
    m = rep["metrics"]
    if not rep.get("benchmark_available"):
        print(f"  ⚠️ {start}~{end} 基准 {BENCHMARK} 无数据，超额不可信", flush=True)
    return {
        "n_symbols": len(e.symbols),
        "benchmark_ok": rep.get("benchmark_available", False),
        "dropped": e.dropped_symbols,
        "days": len(steps),
        "trades": len(e.account.trades),
        "strategy": m["total_return"],
        "benchmark": m["benchmark_return"],
        "alpha": m["alpha"],
        "mdd": m["max_drawdown"],
        "trips": m["round_trips"],
    }


def main() -> None:
    print("=" * 104)
    print("动态股票池 vs 伪分散池 · 对照实验")
    print("=" * 104)

    dynamic = build_dynamic_pool(size=8)
    if not dynamic:
        print("动态池构建失败, 终止")
        return

    print()
    print(f"伪分散池(旧): {OLD_POOL}")
    print(f"动态池(新):   {dynamic}")
    print()
    print("=" * 104)
    print(f"{'场景':<14}{'池':<10}{'成交':>5}{'策略':>10}{'基准':>10}{'超额':>11}")
    print("-" * 104)

    results = {}
    for name, start, end in SCENARIOS:
        for label, pool in (("伪分散", OLD_POOL), ("动态", dynamic)):
            r = run_pool(pool, start, end)
            if r is None:
                continue
            results.setdefault(name, {})[label] = r
            print(
                f"{name if label == '伪分散' else '':<14}{label:<10}"
                f"{r['trades']:>5}{r['strategy']:>9.2%}{r['benchmark']:>10.2%}"
                f"{r['alpha']:>11.2%}",
                flush=True,
            )

    print()
    print("=" * 104)
    print("关键结论")
    print("=" * 104)
    missing = [
        n
        for n, _, _ in SCENARIOS
        if "伪分散" in results.get(n, {}) and not results[n]["伪分散"].get("benchmark_ok")
    ]
    if missing:
        print(f"⚠️ 以下场景基准数据缺失, 超额不可信: {missing}")

    up = results.get("上涨市 2019", {})
    if "伪分散" in up and "动态" in up:
        a_old = up["伪分散"]["alpha"]
        a_new = up["动态"]["alpha"]
        print(f"上涨市 2019 超额:  伪分散池 {a_old:+.2%}  ->  动态池 {a_new:+.2%}")
        print(f"改善幅度: {(a_new - a_old) * 100:+.2f} 个百分点")
        if a_new > a_old + 0.05:
            print("→ 动态池显著改善上涨市表现, 原结论部分来自池子缺陷")
        elif a_new < -0.20:
            print("→ 动态池下上涨市仍大幅跑输, 确认是**策略方向问题**而非池子问题")
        else:
            print("→ 变化不明显, 需更多样本判断")

    print()
    print("全部场景超额对比:")
    print(f"{'场景':<14}{'伪分散池':>12}{'动态池':>12}{'差异':>12}")
    print("-" * 52)
    for name, _, _ in SCENARIOS:
        r = results.get(name, {})
        if "伪分散" in r and "动态" in r:
            a1 = r["伪分散"]["alpha"]
            a2 = r["动态"]["alpha"]
            print(f"{name:<14}{a1:>11.2%}{a2:>12.2%}{(a2 - a1) * 100:>11.2f}")


if __name__ == "__main__":
    main()
