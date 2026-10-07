"""模拟盘 API 路由

端点清单见 ``docs/phase4_prd_paper_trading.md``。

**诚实性约定**
--------------
每个响应都带 ``disclaimer`` 字段明确标注「模拟盘 · 非真实交易」,
且 ``metrics.reliable=False`` 时 ``warnings`` 必非空 —— 前端须显著展示,
不得只挑好看的数字。
"""

from __future__ import annotations

from typing import Any

from loguru import logger

DISCLAIMER = "模拟盘 · 非真实交易, 不构成投资建议"

# 退化股票池: 动态选股不可用时使用。
# ⚠️ 这三只同属银行地产，属"伪分散"，会显著扭曲策略评估结论
# (上涨市踏空 / 下跌市超额虚高)，详见 docs/strategy-evaluation-2026-10-07.md
FALLBACK_SYMBOLS = ["000002", "000001", "600036"]

# 进程内单例(单 worker 场景足够, 与现有 pipeline 状态一致)
_engines: dict[str, Any] = {}


def _get_engine(account_id: str = "default") -> Any:
    """取或建引擎实例"""
    if account_id not in _engines:
        from stock_model.paper.engine import PaperEngine
        from stock_model.strategy.manual import ManualStrategy

        engine = PaperEngine(
            symbols=list(FALLBACK_SYMBOLS),
            initial_capital=10000.0,
            data_source="baostock",
            start_date="20240101",
        )
        engine.add_strategy(ManualStrategy())
        _engines[account_id] = engine
    return _engines[account_id]


def _rebuild_engine(account_id: str, symbols: list[str]):
    """按新股票池重建引擎(保留策略配置)"""
    from stock_model.paper.engine import PaperEngine
    from stock_model.strategy.manual import ManualStrategy

    if not symbols:
        return _get_engine(account_id)
    engine = PaperEngine(
        symbols=symbols,
        initial_capital=10000.0,
        data_source="baostock",
        start_date="20190101",
    )
    engine.add_strategy(ManualStrategy())
    _engines[account_id] = engine
    logger.info(f"股票池已更新: {symbols}")
    return engine


def register_paper_routes(app: Any) -> None:
    """挂载模拟盘相关路由"""

    @app.get("/api/paper/account")
    async def paper_account(account_id: str = "default"):
        """账户总览"""
        engine = _get_engine(account_id)
        acc = engine.account
        return {
            "initial_capital": acc.initial_capital,
            "cash": round(acc.cash, 2),
            "market_value": round(acc.market_value, 2),
            "total_asset": round(acc.total_asset, 2),
            "total_return": round(acc.total_return, 4),
            "position_ratio": round(acc.position_ratio, 4),
            "trading_days": len(acc.equity_curve),
            "disclaimer": DISCLAIMER,
        }

    @app.get("/api/paper/positions")
    async def paper_positions(account_id: str = "default"):
        """当前持仓"""
        engine = _get_engine(account_id)
        items = [p.to_dict() for p in engine.account.positions.values()]
        return {
            "positions": items,
            "count": len(items),
            "disclaimer": DISCLAIMER,
        }

    @app.get("/api/paper/trades")
    async def paper_trades(account_id: str = "default", limit: int = 100):
        """成交记录(倒序)"""
        engine = _get_engine(account_id)
        items = [t.to_dict() for t in engine.account.trades][-limit:][::-1]
        return {
            "trades": items,
            "total": len(engine.account.trades),
            "disclaimer": DISCLAIMER,
        }

    @app.get("/api/paper/metrics")
    async def paper_metrics(account_id: str = "default", benchmark: str = "000002"):
        """绩效指标(含可靠性标记)"""
        engine = _get_engine(account_id)
        report = engine.report(benchmark_symbol=benchmark)
        report["disclaimer"] = DISCLAIMER
        return report

    @app.get("/api/paper/equity")
    async def paper_equity(account_id: str = "default"):
        """资金曲线"""
        engine = _get_engine(account_id)
        items = [p.to_dict() for p in engine.account.equity_curve]
        return {"equity": items, "count": len(items), "disclaimer": DISCLAIMER}

    @app.post("/api/paper/step")
    async def paper_step(payload: dict | None = None):
        """推进一个交易日"""
        engine = _get_engine((payload or {}).get("account_id", "default"))
        try:
            return engine.step()
        except Exception as e:
            logger.error(f"模拟盘推进失败: {e}")
            return {"status": "error", "error": f"{type(e).__name__}: {e}"}

    @app.post("/api/paper/run")
    async def paper_run(payload: dict | None = None):
        """连续推进 N 个交易日

        payload 可传 ``symbols`` 用动态股票池替换默认列表。
        """
        payload = payload or {}
        account_id = payload.get("account_id", "default")
        engine = _get_engine(account_id)

        symbols = payload.get("symbols")
        if symbols:
            # 用调用方指定的股票池重建引擎(动态选股结果)
            engine = _rebuild_engine(account_id, [s.strip() for s in symbols if s.strip()])

        days = int(payload.get("days", 60))
        days = max(1, min(days, 1500))
        try:
            steps = engine.run(days=days)
            return {
                "status": "ok",
                "steps": len(steps),
                "from": steps[0]["date"] if steps else "",
                "to": steps[-1]["date"] if steps else "",
                "trades": len(engine.account.trades),
                "total_asset": round(engine.account.total_asset, 2),
                "symbols": engine.symbols,
                "disclaimer": DISCLAIMER,
            }
        except Exception as e:
            logger.error(f"模拟盘批量执行失败: {e}")
            return {"status": "error", "error": f"{type(e).__name__}: {e}"}

    @app.get("/api/paper/universe")
    async def paper_universe(top_n: int = 20, min_amount: float = 5e7):
        """动态构建候选股票池(替代人工指定的股票列表)

        ⚠️ 耗时较长: 需逐只查询估值/流动性数据, 20-40 只约 1-2 分钟。
        """
        try:
            from stock_model.paper.universe import UniverseConfig, UniverseSelector

            cfg = UniverseConfig(
                top_n=max(5, min(top_n, 50)),
                min_amount=min_amount,
                pre_screen_top=max(40, top_n * 3),
            )
            candidates = UniverseSelector(cfg).build()
            return {
                "candidates": [c.to_dict() for c in candidates],
                "count": len(candidates),
                "config": cfg.describe(),
                "disclaimer": DISCLAIMER,
            }
        except Exception as e:
            logger.error(f"股票池构建失败: {e}")
            return {"error": f"{type(e).__name__}: {str(e)[:200]}", "candidates": []}

    @app.post("/api/paper/reset")
    async def paper_reset(payload: dict | None = None):
        """重置账户"""
        account_id = (payload or {}).get("account_id", "default")
        _engines.pop(account_id, None)
        _get_engine(account_id)
        return {"status": "reset", "disclaimer": DISCLAIMER}
