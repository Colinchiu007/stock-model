"""
Web Dashboard 应用

基于 FastAPI 的 Web 仪表盘，提供:
  - Pipeline 控制 (启动/停止/状态/历史)
  - 实时信号推送 (SSE)
  - 回测执行与结果查询
  - 组合优化
  - 数据质量检查
  - 可视化仪表盘

依赖 fastapi 和 uvicorn (可选，未安装时降级)。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from datetime import datetime
from html import escape as esc
from pathlib import Path
from typing import Any

from loguru import logger

# ---- 数据模型 (模块级，供FastAPI正确解析请求体) ----

try:
    from pydantic import BaseModel

    class SignalRequest(BaseModel):
        symbol: str
        action: str = "HOLD"
        confidence: float = 0.5
        reason: str = ""

    class BacktestRequest(BaseModel):
        symbol: str
        strategy_name: str = "manual"
        start_date: str | None = None
        end_date: str | None = None
        initial_cash: float = 100000.0

    class PipelineRunRequest(BaseModel):
        symbols: list[str] | None = None
        strategy_names: list[str] | None = None

    class PipelineStartRequest(BaseModel):
        interval_minutes: int = 30
        watchlist: list[str] | None = None

except ImportError:
    # pydantic 未安装时，模型不可用
    SignalRequest = None  # type: ignore[assignment, misc]
    BacktestRequest = None  # type: ignore[assignment, misc]
    PipelineRunRequest = None  # type: ignore[assignment, misc]
    PipelineStartRequest = None  # type: ignore[assignment, misc]


def create_app(config: dict | None = None) -> Any:
    """创建 FastAPI 应用实例

    Args:
        config: 可选配置字典

    Returns:
        FastAPI 应用实例

    Raises:
        ImportError: fastapi 未安装时
    """
    try:
        from fastapi import FastAPI, HTTPException, Query
        from fastapi.responses import HTMLResponse
        from fastapi.staticfiles import StaticFiles

        app = FastAPI(
            title="Stock Model Dashboard",
            description="股票分析与投资模型 - Web Dashboard",
            version="4.0.0",
        )

        # 静态资源 (css/js)
        _static_dir = Path(__file__).parent / "static"
        if _static_dir.is_dir():
            app.mount(
                "/static",
                StaticFiles(directory=str(_static_dir)),
                name="static",
            )

        # ---- 应用状态(内存, 生产环境应使用数据库/Redis) ----

        _signals: deque[dict] = deque(maxlen=200)
        _backtest_results: deque[dict] = deque(maxlen=50)
        _pipeline_history: deque[dict] = deque(maxlen=100)
        _pipeline_state: dict = {
            "status": "stopped",
            "started_at": None,
            "interval_minutes": 30,
            "watchlist": [],
            "total_runs": 0,
            "last_run": None,
        }
        _pipeline_instance = None
        _sse_subscribers: list = []
        _background_tasks: set = set()  # 防止异步任务被GC

        # ---- SSE 实时推送 ----

        async def _sse_broadcast(event: str, data: dict) -> None:
            """向所有SSE订阅者广播事件"""
            message = json.dumps({"event": event, "data": data, "ts": datetime.now().isoformat()})
            dead = []
            for i, queue in enumerate(_sse_subscribers):
                try:
                    queue.put_nowait(message)
                except (ValueError, RuntimeError):  # queue full or closed
                    dead.append(i)
            for i in reversed(dead):
                _sse_subscribers.pop(i)

        # ---- Pipeline 集成 API ----

        @app.post("/api/pipeline/start")
        async def start_pipeline(req: PipelineStartRequest):
            """启动Pipeline定时执行"""
            try:
                from stock_model.pipeline.config import PipelineConfig
                from stock_model.pipeline.trading_pipeline import TradingPipeline
                from stock_model.strategy.manual import ManualStrategy

                if _pipeline_state["status"] == "running":
                    return {
                        "status": "already_running",
                        "message": "Pipeline已在运行中",
                    }

                pipeline_config = PipelineConfig(
                    watchlist=req.watchlist or ["000001", "600036"],
                    enable_notify=False,
                    signal_cooldown_minutes=30,
                )
                pipeline = TradingPipeline(config=pipeline_config)
                pipeline.add_strategy(ManualStrategy())

                # 注册回调: 每次执行结果推送到SSE和历史
                def on_result(result):
                    result_dict = {
                        "symbol": result.symbol,
                        "status": result.status.value,
                        "quality_score": result.quality_score,
                        "reason": result.reason,
                        "timestamp": datetime.now().isoformat(),
                    }
                    if result.strategy_result:
                        result_dict["action"] = result.strategy_result.action.value
                        result_dict["confidence"] = result.strategy_result.confidence
                    if result.position_advice:
                        result_dict["position"] = result.position_advice
                    _pipeline_history.append(result_dict)
                    _pipeline_state["total_runs"] += 1
                    _pipeline_state["last_run"] = datetime.now().isoformat()
                    # 异步广播(在事件循环中)
                    try:
                        loop = asyncio.get_event_loop()
                        task = loop.create_task(_sse_broadcast("pipeline_result", result_dict))
                        _background_tasks.add(task)
                        task.add_done_callback(_background_tasks.discard)
                    except RuntimeError:
                        pass

                pipeline.on_result(on_result)
                pipeline.start_scheduled(interval_minutes=req.interval_minutes)

                # 确认调度器确实在跑, 再更新状态。
                # start_scheduled 成功返回即意味着 collector 已进入运行态;
                # 这里再校验一次, 防止未来新增的静默降级路径把
                # "未启动" 伪装成 "已启动"。
                if not pipeline.is_running:
                    pipeline.stop()
                    raise RuntimeError(
                        "Pipeline启动失败: 调度器未进入运行状态"
                        "(可能未安装 apscheduler, 请执行 pip install apscheduler)"
                    )

                # 保存实例引用
                nonlocal _pipeline_instance
                _pipeline_instance = pipeline

                _pipeline_state.update(
                    {
                        "status": "running",
                        "started_at": datetime.now().isoformat(),
                        "interval_minutes": req.interval_minutes,
                        "watchlist": req.watchlist or ["000001", "600036"],
                    }
                )

                return {
                    "status": "started",
                    "message": f"Pipeline已启动, 间隔{req.interval_minutes}分钟",
                    "watchlist": _pipeline_state["watchlist"],
                }

            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Pipeline启动失败: {e}")
                raise HTTPException(status_code=500, detail=str(e)) from e

        @app.post("/api/pipeline/stop")
        async def stop_pipeline():
            """停止Pipeline定时执行"""
            nonlocal _pipeline_instance
            if _pipeline_state["status"] != "running" or _pipeline_instance is None:
                return {"status": "not_running", "message": "Pipeline未在运行"}

            try:
                _pipeline_instance.stop()
                _pipeline_state.update(
                    {
                        "status": "stopped",
                        "started_at": None,
                    }
                )
                _pipeline_instance = None
                return {"status": "stopped", "message": "Pipeline已停止"}
            except Exception as e:
                logger.error(f"Pipeline停止失败: {e}")
                raise HTTPException(status_code=500, detail=str(e)) from e

        @app.get("/api/pipeline/status")
        async def pipeline_status():
            """获取Pipeline状态"""
            state = _pipeline_state.copy()
            if _pipeline_instance is not None:
                try:
                    state["stats"] = _pipeline_instance.stats
                except (ValueError, RuntimeError):  # queue full or closed
                    state["stats"] = None
            return state

        @app.post("/api/pipeline/run")
        async def pipeline_run_once(req: PipelineRunRequest):
            """手动触发Pipeline单次/批量执行"""
            try:
                from stock_model.pipeline.config import PipelineConfig
                from stock_model.pipeline.trading_pipeline import TradingPipeline
                from stock_model.strategy.manual import ManualStrategy

                symbols = req.symbols or ["000001"]
                config = PipelineConfig(
                    watchlist=symbols,
                    enable_notify=False,
                    signal_cooldown_minutes=0,
                )
                pipeline = TradingPipeline(config=config)
                pipeline.add_strategy(ManualStrategy())

                start_time = time.time()
                summary = pipeline.run_batch(symbols)
                elapsed = time.time() - start_time

                result_dict = {
                    "total": summary.total,
                    "executed": summary.executed,
                    "skipped": summary.skipped,
                    "blocked": summary.blocked,
                    "errors": summary.errors,
                    "success_rate": summary.success_rate,
                    "run_time": elapsed,
                    "results": [
                        {
                            "symbol": r.symbol,
                            "status": r.status.value,
                            "quality_score": r.quality_score,
                            "reason": r.reason,
                            "action": (
                                r.strategy_result.action.value if r.strategy_result else None
                            ),
                            "confidence": (
                                r.strategy_result.confidence if r.strategy_result else None
                            ),
                            "position": r.position_advice,
                        }
                        for r in summary.results
                    ],
                    "timestamp": datetime.now().isoformat(),
                }
                _pipeline_history.append(result_dict)

                return result_dict

            except Exception as e:
                logger.error(f"Pipeline手动执行失败: {e}")
                raise HTTPException(status_code=500, detail=str(e)) from e

        @app.get("/api/pipeline/history")
        async def pipeline_history(limit: int = Query(default=20, ge=1, le=100)):
            """获取Pipeline运行历史"""
            items = list(_pipeline_history)[-limit:]
            return {"history": items, "total": len(_pipeline_history)}

        # ---- SSE 实时推送端点 ----

        @app.get("/api/events")
        async def sse_events():
            """SSE实时事件流"""
            from fastapi.responses import StreamingResponse

            queue: asyncio.Queue = asyncio.Queue()
            _sse_subscribers.append(queue)

            async def event_generator():
                try:
                    # 发送初始连接事件
                    yield f"data: {json.dumps({'event': 'connected', 'ts': datetime.now().isoformat()})}\n\n"
                    while True:
                        try:
                            message = await asyncio.wait_for(queue.get(), timeout=30)
                            yield f"data: {message}\n\n"
                        except asyncio.TimeoutError:
                            # 心跳
                            yield f"data: {json.dumps({'event': 'heartbeat', 'ts': datetime.now().isoformat()})}\n\n"
                except asyncio.CancelledError:
                    pass
                finally:
                    if queue in _sse_subscribers:
                        _sse_subscribers.remove(queue)

            return StreamingResponse(
                event_generator(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        # ---- 原有API (增强) ----

        @app.get("/", response_class=HTMLResponse)
        async def dashboard():
            """仪表盘首页"""
            return _render_dashboard()

        @app.get("/api/health")
        async def health_check():
            """健康检查"""
            return {
                "status": "ok",
                "version": "4.0.0",
                "pipeline_status": _pipeline_state["status"],
                "timestamp": datetime.now().isoformat(),
            }

        @app.get("/api/signals")
        async def get_signals(limit: int = Query(default=20, ge=1, le=200)):
            """获取最近信号"""
            items = list(_signals)[-limit:]
            return {"signals": items, "total": len(_signals)}

        @app.post("/api/signals")
        async def add_signal(req: SignalRequest):
            """添加信号"""
            signal = {
                "symbol": req.symbol,
                "action": req.action,
                "confidence": req.confidence,
                "reason": req.reason,
                "timestamp": datetime.now().isoformat(),
            }
            _signals.append(signal)
            # 广播到SSE
            await _sse_broadcast("signal", signal)
            return {"status": "ok", "signal": signal}

        @app.post("/api/backtest")
        async def run_backtest(req: BacktestRequest):
            """运行回测"""
            try:
                from stock_model.data.fetcher import StockDataFetcher
                from stock_model.strategy.engine import BacktestEngine
                from stock_model.strategy.manual import ManualStrategy

                fetcher = StockDataFetcher()
                df = fetcher.get_daily(req.symbol, start_date=req.start_date)
                if df is None or df.empty:
                    raise HTTPException(
                        status_code=404,
                        detail=f"无法获取 {req.symbol} 数据",
                    )

                strategy = ManualStrategy()
                engine = BacktestEngine(initial_cash=req.initial_cash)
                result = engine.run(strategy, df, req.symbol)

                result_dict = {
                    "symbol": req.symbol,
                    "strategy": req.strategy_name,
                    "initial_cash": result.initial_cash,
                    "final_cash": result.final_cash,
                    "metrics": result.metrics,
                    "trades_count": len(result.trades),
                    "timestamp": datetime.now().isoformat(),
                }
                _backtest_results.append(result_dict)
                # 广播到SSE
                await _sse_broadcast("backtest", result_dict)
                return result_dict

            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"回测失败: {e}")
                raise HTTPException(status_code=500, detail=str(e)) from e

        @app.get("/api/backtest/results")
        async def get_backtest_results(
            limit: int = Query(default=10, ge=1, le=50),
        ):
            """获取回测结果"""
            items = list(_backtest_results)[-limit:]
            return {"results": items, "total": len(_backtest_results)}

        @app.get("/api/portfolio/optimize")
        async def optimize_portfolio(
            symbols: str = "000001,000002",
            method: str = "equal_weight",
        ):
            """组合优化"""
            try:
                from stock_model.data.fetcher import StockDataFetcher
                from stock_model.portfolio.optimizer import PortfolioOptimizer

                symbol_list = [s.strip() for s in symbols.split(",")]
                fetcher = StockDataFetcher()
                prices_dict = {}
                returns_dict = {}

                for sym in symbol_list:
                    df = fetcher.get_daily(sym)
                    if df is not None and not df.empty:
                        prices_dict[sym] = df["close"].iloc[-1]
                        if len(df) > 1:
                            returns_dict[sym] = df["close"].pct_change().dropna()

                if not prices_dict:
                    raise HTTPException(status_code=404, detail="无法获取数据")

                optimizer = PortfolioOptimizer()
                method_map = {
                    "equal_weight": optimizer.equal_weight,
                    "risk_parity": optimizer.risk_parity,
                    "min_variance": optimizer.min_variance,
                    "mean_variance": optimizer.mean_variance,
                }

                opt_func = method_map.get(method, optimizer.equal_weight)
                if method in ("risk_parity", "min_variance", "mean_variance"):
                    portfolio = opt_func(
                        returns=returns_dict,
                        prices=prices_dict,
                        total_value=100000,
                    )
                else:
                    portfolio = opt_func(
                        symbols=symbol_list,
                        prices=prices_dict,
                        total_value=100000,
                    )

                return {
                    "method": method,
                    "weights": {w.symbol: w.weight for w in portfolio.weights},
                    "total_value": portfolio.total_value,
                }

            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"组合优化失败: {e}")
                raise HTTPException(status_code=500, detail=str(e)) from e

        @app.get("/api/quality/{symbol}")
        async def check_data_quality(symbol: str):
            """检查数据质量"""
            try:
                from stock_model.data.fetcher import StockDataFetcher
                from stock_model.data.monitor import DataQualityMonitor

                fetcher = StockDataFetcher()
                df = fetcher.get_daily(symbol)
                if df is None or df.empty:
                    raise HTTPException(
                        status_code=404,
                        detail=f"无法获取 {symbol} 数据",
                    )

                monitor = DataQualityMonitor()
                report = monitor.check(df, symbol)
                return {
                    "symbol": symbol,
                    "score": report.score,
                    "issues": report.issues,
                }

            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"数据质量检查失败: {e}")
                raise HTTPException(status_code=500, detail=str(e)) from e

        # ---- 选股/分析路由 (独立模块, 避免本文件继续膨胀) ----
        from stock_model.web.screener import register_screener_routes

        register_screener_routes(app)

        return app

    except ImportError as err:
        logger.warning(
            "fastapi 或 uvicorn 未安装，Web Dashboard 不可用。请安装: pip install stock-model[web]"
        )
        raise ImportError(
            "Web Dashboard 需要 fastapi 和 uvicorn。请安装: pip install stock-model[web]"
        ) from err


def _render_dashboard() -> str:
    """渲染仪表盘 HTML

    页面本体位于 static/index.html, 这里只负责读取并返回。
    历史上此处内嵌了 280+ 行 HTML 字符串, 难以维护与测试,
    现已拆分到 static/ 下的 html/css/js 三个文件。
    """
    static_dir = Path(__file__).parent / "static"
    index = static_dir / "index.html"
    try:
        return index.read_text(encoding="utf-8")
    except OSError as err:
        logger.error(f"读取前端页面失败 {index}: {err}")
        return (
            '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">'
            '<title>Stock Model</title></head><body style="font-family:sans-serif;padding:40px">'
            "<h1>前端页面加载失败</h1>"
            f"<p>找不到 {esc(str(index))}</p>"
            "<p>请确认已完整克隆仓库（static/ 目录需随包安装）。</p>"
            "</body></html>"
        )
