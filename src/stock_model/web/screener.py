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
from typing import TYPE_CHECKING, Any

import pandas as pd
from loguru import logger

if TYPE_CHECKING:
    from collections.abc import Callable

import threading
import time
import uuid

from stock_model.data.fetcher import StockDataFetcher

# 扫描进度: request_id -> {"done","total","updated_at"}。
# 只为消除前端"黑屏 60 秒"的困惑, 不参与业务判断。
_SCAN_PROGRESS_TTL = 300.0  # 秒; 过期记录在查询/结束时清理
_scan_progress: dict[str, dict] = {}
_progress_lock = threading.Lock()

# 按 data_source 复用 fetcher(见 _fetcher_for 的说明)
_fetcher_cache: dict[str, Any] = {}
_fetcher_lock = threading.Lock()


def _prune_progress() -> None:
    """清掉过期的扫描进度记录"""
    now = time.monotonic()
    with _progress_lock:
        for rid in [
            rid for rid, v in _scan_progress.items() if now - v.get("_ts", 0) > _SCAN_PROGRESS_TTL
        ]:
            _scan_progress.pop(rid, None)


def _fetcher_for(data_source: str):
    """按数据源名复用 fetcher 实例(整批扫描共用一个)

    为什么必须复用(2026-10-19 实测): ``StockDataFetcher(source=...)`` 的构造
    会实例化主数据源, 而 ``BaostockSource.__init__`` 里就``login()`` ——
    数据源不可用时每次构造要等超时(实测 21~94 秒/只)。原本每只股票新建一个,
    扫 5 只 = 350 秒, 而缓存命中后取数只要 0.1 秒, **99% 的时间在等 login 超时**。

    baostock 单连接**不允许并发查询**, 但扫描本身是串行的, 复用实例安全。

    用 dict 按源名缓存: 不同 ``data_source`` 参数各自一个, 不会串味。
    """
    with _fetcher_lock:
        f = _fetcher_cache.get(data_source)
        if f is None:
            f = StockDataFetcher(source=data_source)
            _fetcher_cache[data_source] = f
        return f


def _scan_with_progress(
    symbol_list: list[str],
    data_source: str,
    start_date: str,
    strategy_cls: Callable[[], Any],
    min_rows: int,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[dict]:
    """串行扫描并逐只上报进度

    进度回调用来消除"扫 30 只黑屏 60 秒"的空窗感 —— 前端把
    ``on_progress`` 接到一个轮询端点即可显示"已扫描 12/30"。

    串行的理由(见 register_routes 里同样的注释): baostock 是全局单连接,
    并发查询会互相抢占响应导致 RuntimeError(实测复现)。正确性优先。
    """
    items: list[dict] = []
    total = len(symbol_list)
    for i, sym in enumerate(symbol_list, 1):
        items.append(_analyze_one(sym, data_source, start_date, strategy_cls, min_rows))
        if on_progress is not None:
            try:
                on_progress(i, total)
            except Exception as e:  # 进度上报失败绝不能影响扫描结果
                logger.debug(f"进度回调异常(已忽略): {type(e).__name__}: {e}")
    return items


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
    strategy_cls: Callable[[], Any],
    min_rows: int,
) -> dict:
    """分析单只股票, 返回结构化结果

    任何单只股票的失败都被降级为一条错误记录, 不影响批量扫描。
    """
    from stock_model.analysis.signals import SignalGenerator
    from stock_model.analysis.technical import TechnicalAnalysis

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
        fetcher = _fetcher_for(data_source)
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

    # 以下三个端点都是**同步阻塞**(内部拉真实行情, 数十秒):
    # 必须用 `def` 而非 `async def` —— FastAPI 会把 def 端点丢进线程池,
    # async def 则直接在事件循环上执行, 同步 IO 会**冻结整个事件循环**,
    # 期间所有请求(含其它用户的)全部排队(实测: 扫描时点停止挂 10s+)。
    @app.get("/api/screener")
    def screener(
        symbols: str = "000001,000002,600036,000651,600519",
        data_source: str = "baostock",
        start_date: str = "20240101",
        min_rows: int = 60,
        limit: int = 20,
        sort_by: str = "score",
        request_id: str = "",
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

        # 进度上报: 让前端能显示"已扫描 N/M", 消除 60 秒黑屏的困惑。
        # request_id 由调用方(前端)生成: 请求还在飞的时候它就已经知道
        # 该去轮询哪个 id 了 —— 用响应里的 id 就晚了(响应=扫描结束)。
        rid = (request_id or "").strip()[:32] or uuid.uuid4().hex[:12]
        request_id = rid

        def _on_progress(done: int, total: int) -> None:
            with _progress_lock:
                _scan_progress[request_id] = {
                    "done": done,
                    "total": total,
                    "updated_at": datetime.now().isoformat(),
                    "_ts": time.monotonic(),
                }

        with _progress_lock:
            _scan_progress[request_id] = {
                "done": 0,
                "total": len(symbol_list),
                "updated_at": datetime.now().isoformat(),
                "_ts": time.monotonic(),
            }

        try:
            # 串行而非并发: baostock 是全局单连接, 其 query_* 接口不线程安全,
            # 并发查询会互相抢占响应导致 RuntimeError(实测复现)。
            # akshare 走 HTTP 本身可以并发, 但数据源由调用方指定, 无法在此区分,
            # 因此统一串行 —— 扫描数量有限时耗时可接受, 正确性优先。
            # (提速靠缓存预热 + TTL, 不靠并发 —— 见 docs/SCREENER_PERF.md)
            items = _scan_with_progress(
                symbol_list,
                data_source,
                start_date,
                ManualStrategy,
                min_rows,
                on_progress=_on_progress,
            )
        finally:
            # 进度记录保留一段时间供前端读取最终状态, 过期由查询时清理
            _prune_progress()

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
                "request_id": request_id,
            },
        }

    @app.get("/api/screener/progress")
    def screener_progress(request_id: str) -> dict:
        """查询某次扫描的进度(前端轮询用)

        返回 ``{"request_id","done","total","finished"}``。
        未知/过期的 request_id 返回 ``finished=True``(前端据此停止轮询),
        而不是 404 —— 轮询端点的"查不到"是正常状态, 不是错误。
        """
        _prune_progress()
        with _progress_lock:
            rec = _scan_progress.get(request_id)
        if rec is None:
            return {"request_id": request_id, "done": 0, "total": 0, "finished": True}
        done = int(rec.get("done", 0))
        total = int(rec.get("total", 0))
        return {
            "request_id": request_id,
            "done": done,
            "total": total,
            "finished": total > 0 and done >= total,
            "updated_at": rec.get("updated_at"),
        }

    @app.get("/api/analyze/{symbol}")
    def analyze_detail(
        symbol: str,
        data_source: str = "baostock",
        start_date: str = "20240101",
    ) -> dict:
        """单只股票的完整分析明细(含指标快照)"""
        from stock_model.analysis.technical import TechnicalAnalysis
        from stock_model.data.monitor import DataQualityMonitor

        try:
            fetcher = _fetcher_for(data_source)
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
    def backtest_detail(
        symbol: str,
        data_source: str = "baostock",
        start_date: str = "20240101",
        initial_cash: float = 100000.0,
    ) -> dict:
        """回测详情, 含完整交易记录(用于展示"最近买卖")"""
        from stock_model.strategy.engine import BacktestEngine
        from stock_model.strategy.manual import ManualStrategy

        try:
            fetcher = _fetcher_for(data_source)
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
