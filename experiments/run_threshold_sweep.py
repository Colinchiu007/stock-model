"""策略参数敏感性实验

回答: ManualStrategy 的问题是「阈值太严」还是「方向不对」?

做法: 把硬编码的 net_score 阈值参数化, 在不同市场环境下
扫描阈值 1~4, 看超额收益如何变化。

若放宽阈值能在上涨市改善超额 → 问题是阈值, 可简单修复
若放宽后反而更差       → 问题是策略方向, 调参救不了

用法: python experiments/run_threshold_sweep.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd

from stock_model.analysis.signals import SignalGenerator
from stock_model.paper.engine import PaperEngine

SYMBOLS = ["000002", "000001", "600036"]
BENCHMARK = "000002"
WEIGHTS = {"weak": 1, "medium": 2, "strong": 3}


class ThresholdStrategy:
    """ManualStrategy 的阈值可调版本

    复刻 manual.py 的决策逻辑, 唯一区别是 threshold 可传入。
    刻意不继承 ManualStrategy —— 它在 __init__ 里就固定了阈值,
    继承无法覆盖。
    """

    def __init__(self, threshold: int = 3):
        from stock_model.analysis.technical import TechnicalAnalysis

        self.threshold = threshold
        self.ta = TechnicalAnalysis()
        self.sg = SignalGenerator()
        self.name = f"manual_t{threshold}"

    def analyze(self, symbol: str, df: pd.DataFrame):
        from stock_model.strategy.base import ActionType, StrategyResult

        enriched = self.ta.analyze_all(df)
        signals = self.sg.generate_all(enriched, symbol)

        buy = sum(WEIGHTS[s.strength.value] for s in signals if s.signal_type.value == "buy")
        sell = sum(WEIGHTS[s.strength.value] for s in signals if s.signal_type.value == "sell")
        net = buy - sell
        th = self.threshold

        if net >= th:
            action, conf = ActionType.BUY, min(0.95, 0.6 + net * 0.05)
        elif net <= -th:
            action, conf = ActionType.SELL, min(0.95, 0.6 + abs(net) * 0.05)
        else:
            action, conf = ActionType.HOLD, 0.5

        close = float(df["close"].iloc[-1])
        return StrategyResult(
            symbol=symbol,
            action=action,
            confidence=conf,
            reason=f"net={net}(阈值{th}) 买{buy}/卖{sell}",
            position_pct=max(5.0, abs(net) * 8.0),
        )


def run(start: str, end: str, threshold: int) -> dict:
    e = PaperEngine(
        symbols=SYMBOLS,
        initial_capital=10000.0,
        data_source="baostock",
        start_date=start,
        end_date=end,
        min_data_rows=60,
    )
    e.add_strategy(ThresholdStrategy(threshold))
    steps = e.run()
    if not steps:
        return {"error": "无数据"}
    rep = e.report(benchmark_symbol=BENCHMARK)
    m = rep["metrics"]
    return {
        "days": len(steps),
        "trades": len(e.account.trades),
        "strategy": m["total_return"],
        "benchmark": m["benchmark_return"],
        "alpha": m["alpha"],
        "mdd": m["max_drawdown"],
        "trips": m["round_trips"],
    }


SCENARIOS = [
    ("上涨市 2019", "20190101", "20191231"),
    ("下跌市 2021", "20210101", "20211231"),
    ("下跌市 2023", "20230101", "20231231"),
    ("下跌市 2024", "20240101", "20241231"),
]


def main() -> None:
    print("=" * 96)
    print("阈值敏感性扫描 · 1万元 · 标的 000002/000001/600036")
    print("=" * 96)

    results: dict[str, dict[int, dict]] = {}
    for name, start, end in SCENARIOS:
        results[name] = {}
        print(f"\n>>> {name}", flush=True)
        for th in (1, 2, 3):
            r = run(start, end, th)
            results[name][th] = r
            if r.get("error"):
                print(f"    阈值{th}: 失败")
            else:
                print(
                    f"    阈值{th}: {r['trades']:>3}笔 | "
                    f"策略{r['strategy']:+.2%} 基准{r['benchmark']:+.2%} "
                    f"超额{r['alpha']:+.2%}",
                    flush=True,
                )

    print("\n" + "=" * 96)
    print("汇总: 超额收益 Alpha")
    print("=" * 96)
    print(f"{'场景':<16}" + "".join(f"{'阈值±' + str(t):>14}" for t in (1, 2, 3)))
    print("-" * 96)
    for name, _, _ in SCENARIOS:
        row = f"{name:<16}"
        for th in (1, 2, 3):
            r = results[name][th]
            row += f"{r['alpha']:>13.2%} " if not r.get("error") else f"{'—':>14}"
        print(row)

    print("\n" + "=" * 96)
    print("结论")
    print("=" * 96)
    up = results["上涨市 2019"]
    print(f"上涨市: 阈值±1 {up[1]['alpha']:+.2%}  →  ±3 {up[3]['alpha']:+.2%}")
    if up[1]["alpha"] > up[3]["alpha"]:
        print("→ 放宽阈值能改善上涨市表现, 说明「阈值过严」是部分原因")
    else:
        print("→ 放宽阈值在上涨市也无效, 说明问题是策略方向而非参数")

    all_alp1 = [results[n][1]["alpha"] for n, _, _ in SCENARIOS]
    all_alp3 = [results[n][3]["alpha"] for n, _, _ in SCENARIOS]
    print(f"四场景平均超额: 阈值±1 {sum(all_alp1) / 4:+.2%}  →  ±3 {sum(all_alp3) / 4:+.2%}")


if __name__ == "__main__":
    main()
