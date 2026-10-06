"""选股与信号分析 API 路由

提供前端仪表盘所需的数据接口:
  GET /api/screener      批量扫描并返回按信号强度排序的选股榜
  GET /api/analyze/{sym} 单只股票的完整分析明细
  GET /api/backtest/{sym} 单只股票的回测详情（含交易记录）

设计要点:
  - 选股榜直接跑 StrategyEngine，返回排序后的结果，
    不再依赖前端先跑 pipeline 再自行聚合
  - 每个标的附带触发信号列表，便于前端展示"为什么推荐"
  - 扫描有并发上限，避免一次性拉几十只股票把数据源打挂
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd
from loguru import logger


def _signal_to_dict(signal: Any) -> dict:
    """Signal 对象转 dict"""
    return {
        "type": signal.signal_type.value,
        "strength": signal.strength.value,
        "reason": signal.reason,
        "price": signal.price,
        "timestamp": signal.timestamp.isoformat() if signal.timestamp else None,
    }


def _analyze_one(
    symbol: str,
    data_source: str,
    start_date: str,
    strategy_cls: type,
    min_rows: int,
) -> dict:
    """分析单只股票, 返回结构化结果

    任何单只股票的失败都被降级为一条错误记录, 不影响批量扫描。
    """
    from stock_model.analysis.signals import SignalGenerator
    from stock_model.analysis.technical import TechnicalAnalysis
    from stock_model.data.fetcher import StockDataFetcher

    result: dict[str, Any] = {
        "symbol": symbol,
        "action": "hold",
        "confidence": 0.0,
        "target_price": None,
        "stop_loss": None,
        "position_pct": 0.0,
        "reason": "",
        "signals": [],
        "score": 0.0,
        "close": None,
        "quality_score": None,
        "error": None,
        "analyzed_at": datetime.now().isoformat(),
    }

    try:
        fetcher = StockDataFetcher(source=data_source)
        df = fetcher.get_daily(symbol, start_date=start_date)
        if df is None or df.empty:
            result["error"] = "无数据"
            return result
        if len(df) < min_rows:
            result["error"] = f"数据不足({len(df)}行)"
            return result

        df = TechnicalAnalysis().analyze_all(df)
        signals = SignalGenerator().generate_all(df, symbol)

        strategy = strategy_cls()
        sr = strategy.analyze(symbol, df)

        # 评分: 用于前端排序。信号强度加权 + 策略信心度
        weight = {"weak": 1, "medium": 2, "strong": 3}
        action_sign = {"buy": 1, "sell": -1, "hold": 0}
        raw = sum(
            action_sign.get(s.signal_type.value, 0) * weight.get(s.strength.value, 1)
            for s in signals
        )
        result["score"] = round(raw + (sr.confidence * 2 * action_sign.get(sr.action.value, 0)), 3)

        result.update(
            {
                "action": sr.action.value,
                "confidence": round(sr.confidence, 3),
                "target_price": sr.target_price,
                "stop_loss": sr.stop_loss,
                "position_pct": round(sr.position_pct, 1),
                "reason": sr.reason,
                "signals": [_signal_to_dict(s) for s in signals],
                "close": round(float(df["close"].iloc[-1]), 3),
                "trend": sr.metadata.get("trend"),
            }
        )
        return result
    except Exception as e:
        logger.warning(f"分析 {symbol} 失败: {type(e).__name__}: {e}")
        result["error"] = f"{type(e).__name__}: {str(e)[:120]}"
        return result


def register_screener_routes(app: Any) -> None:
    """把选股相关路由挂到 FastAPI app 上"""

    @app.get("/api/screener")
    async def screener(
        symbols: str = "000001,000002,600036,000651,600519",
        data_source: str = "baostock",
        start_date: str = "20240101",
        min_rows: int = 60,
        limit: int = 20,
        sort_by: str = "score",
    ) -> dict:
        """批量扫描选股榜

        Args:
            symbols: 逗号分隔的股票代码
            data_source: akshare / baostock
            start_date: 数据起始日期 YYYYMMDD
            min_rows: 最少数据行数
            limit: 返回条数
            sort_by: score / confidence / symbol

        Returns:
            {"results": [...], "summary": {...}}
        """
        from stock_model.strategy.manual import ManualStrategy

        symbol_list = [s.strip() for s in symbols.split(",") if s.strip()][:60]
        if not symbol_list:
            return {"results": [], "summary": {"total": 0}}

        # 串行而非并发: baostock 是全局单连接, 其 query_* 接口不线程安全,
        # 并发查询会互相抢占响应导致 RuntimeError(实测复现)。
        # akshare 走 HTTP 本身可以并发, 但数据源由调用方指定, 无法在此区分,
        # 因此统一串行 —— 扫描数量有限时耗时可接受, 正确性优先。
        items: list[dict] = [
            _analyze_one(sym, data_source, start_date, ManualStrategy, min_rows)
            for sym in symbol_list
        ]

        # 排序: 分数降序, 失败项沉底
        key_map = {
            "score": lambda r: -r["score"],
            "confidence": lambda r: -r["confidence"],
            "symbol": lambda r: r["symbol"],
        }
        items.sort(key=key_map.get(sort_by, key_map["score"]))
        items = items[:limit]

        action_counts: dict[str, int] = {}
        for item in items:
            action_counts[item["action"]] = action_counts.get(item["action"], 0) + 1

        return {
            "results": items,
            "summary": {
                "total": len(items),
                "scanned": len(symbol_list),
                "failed": sum(1 for r in items if r["error"]),
                "action_counts": action_counts,
                "generated_at": datetime.now().isoformat(),
            },
        }

    @app.get("/api/analyze/{symbol}")
    async def analyze_detail(
        symbol: str,
        data_source: str = "baostock",
        start_date: str = "20240101",
    ) -> dict:
        """单只股票的完整分析明细(含指标快照)"""
        from stock_model.analysis.technical import TechnicalAnalysis
        from stock_model.data.fetcher import StockDataFetcher
        from stock_model.data.monitor import DataQualityMonitor

        try:
            fetcher = StockDataFetcher(source=data_source)
            df = fetcher.get_daily(symbol, start_date=start_date)
            if df is None or df.empty:
                return {"symbol": symbol, "error": "无数据"}

            report = DataQualityMonitor().check(df, symbol=symbol)
            enriched = TechnicalAnalysis().analyze_all(df)
            last = enriched.iloc[-1]

            def _val(col: str) -> float | None:
                if col not in enriched.columns:
                    return None
                val = last[col]
                if isinstance(val, pd.Series):
                    val = val.iloc[0]
                try:
                    fval = float(val)
                except (TypeError, ValueError):
                    return None
                return None if pd.isna(fval) else round(fval, 4)

            snapshot = {
                "close": _val("close"),
                "volume": _val("volume"),
                "ma5": _val("ma5"),
                "ma20": _val("ma20"),
                "ma60": _val("ma60"),
                "rsi14": _val("RSI_14"),
                "macd": _val("MACD_12_26_9"),
                "macd_signal": _val("MACDs_12_26_9"),
                "macd_hist": _val("MACDh_12_26_9"),
                "boll_upper": _val("BBU_20_2.0"),
                "boll_mid": _val("BBM_20_2.0"),
                "boll_lower": _val("BBL_20_2.0"),
                "atr14": _val("atr14"),
                "kdj_k": _val("kdj_k"),
                "kdj_d": _val("kdj_d"),
                "kdj_j": _val("kdj_j"),
                "obv": _val("obv"),
            }

            base = _analyze_one(symbol, data_source, start_date, _default_strategy, 1)

            return {
                "symbol": symbol,
                "indicators": snapshot,
                "analysis": base,
                "quality": {
                    "score": report.score,
                    "issues": [
                        {
                            "severity": i.severity,
                            "category": i.category,
                            "message": i.message,
                        }
                        for i in report.issues[:10]
                    ],
                },
                "history": [
                    {"date": str(idx.date()), "close": round(float(row["close"]), 3)}
                    for idx, row in df.tail(60).iterrows()
                ],
            }
        except Exception as e:
            logger.error(f"分析 {symbol} 失败: {e}")
            return {"symbol": symbol, "error": f"{type(e).__name__}: {str(e)[:200]}"}

    @app.get("/api/backtest/{symbol}")
    async def backtest_detail(
        symbol: str,
        data_source: str = "baostock",
        start_date: str = "20240101",
        initial_cash: float = 100000.0,
    ) -> dict:
        """回测详情, 含完整交易记录(用于展示"最近买卖")"""
        from stock_model.data.fetcher import StockDataFetcher
        from stock_model.strategy.engine import BacktestEngine
        from stock_model.strategy.manual import ManualStrategy

        try:
            fetcher = StockDataFetcher(source=data_source)
            df = fetcher.get_daily(symbol, start_date=start_date)
            if df is None or df.empty:
                return {"symbol": symbol, "error": "无数据"}

            result = BacktestEngine(initial_cash=initial_cash).run(ManualStrategy(), df, symbol)

            trades = [
                {
                    "action": t.action.value,
                    "price": round(t.price, 3),
                    "shares": t.shares,
                    "amount": round(t.amount, 2),
                    "commission": round(t.commission, 2),
                    "timestamp": str(t.timestamp),
                }
                for t in result.trades
            ]

            ta_ = result.trade_analysis
            bm = result.benchmark

            return {
                "symbol": symbol,
                "metrics": result.metrics,
                "total_return": round(result.total_return, 4),
                "initial_cash": result.initial_cash,
                "final_cash": round(result.final_cash, 2),
                "trades": trades,
                "trade_count": len(trades),
                "trade_analysis": {
                    "total_pairs": ta_.total_pairs,
                    "win_count": ta_.win_count,
                    "loss_count": ta_.loss_count,
                    "win_rate": round(ta_.win_rate, 4),
                    "avg_return": round(ta_.avg_return, 4),
                    "avg_holding_days": round(ta_.avg_holding_days, 1),
                    "profit_factor": round(ta_.profit_factor, 3)
                    if ta_.profit_factor != float("inf")
                    else None,
                },
                "benchmark": {
                    "strategy_return": round(bm.strategy_return, 4),
                    "benchmark_return": round(bm.benchmark_return, 4),
                    "alpha": round(bm.alpha, 4),
                    "beta": round(bm.beta, 4),
                },
                "equity_curve": [
                    {"index": i, "equity": round(float(v), 2)}
                    for i, v in result.equity_curve.items()
                ],
            }
        except Exception as e:
            logger.error(f"回测 {symbol} 失败: {e}")
            return {"symbol": symbol, "error": f"{type(e).__name__}: {str(e)[:200]}"}


def _default_strategy():
    from stock_model.strategy.manual import ManualStrategy

    return ManualStrategy()
