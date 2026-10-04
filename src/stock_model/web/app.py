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
from typing import Any

from loguru import logger


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
        from pydantic import BaseModel

        app = FastAPI(
            title="Stock Model Dashboard",
            description="股票分析与投资模型 - Web Dashboard",
            version="3.0.0",
        )

        # ---- 数据模型 ----

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

        # ---- SSE 实时推送 ----

        async def _sse_broadcast(event: str, data: dict) -> None:
            """向所有SSE订阅者广播事件"""
            message = json.dumps({"event": event, "data": data, "ts": datetime.now().isoformat()})
            dead = []
            for i, queue in enumerate(_sse_subscribers):
                try:
                    queue.put_nowait(message)
                except Exception:
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
                        loop.create_task(_sse_broadcast("pipeline_result", result_dict))
                    except RuntimeError:
                        pass

                pipeline.on_result(on_result)
                pipeline.start_scheduled(interval_minutes=req.interval_minutes)

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

            except ImportError as e:
                raise HTTPException(status_code=500, detail=f"依赖缺失: {e}") from e
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
                except Exception:
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
                "version": "3.0.0",
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

        return app

    except ImportError:
        logger.warning(
            "fastapi 或 uvicorn 未安装，Web Dashboard 不可用。请安装: pip install stock-model[web]"
        )
        raise ImportError(
            "Web Dashboard 需要 fastapi 和 uvicorn。请安装: pip install stock-model[web]"
        )


def _render_dashboard() -> str:
    """渲染仪表盘HTML"""
    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Stock Model Dashboard</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f0f2f5; color: #333; }
        .header { background: linear-gradient(135deg, #1a73e8, #4285f4); color: white; padding: 20px 24px; display: flex; justify-content: space-between; align-items: center; }
        .header h1 { font-size: 22px; font-weight: 600; }
        .header .version { opacity: 0.8; font-size: 13px; }
        .container { max-width: 1200px; margin: 0 auto; padding: 16px; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 16px; margin-bottom: 16px; }
        .card { background: white; border-radius: 12px; padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
        .card h2 { font-size: 15px; color: #666; margin-bottom: 12px; text-transform: uppercase; letter-spacing: 0.5px; }
        .card .value { font-size: 32px; font-weight: 700; color: #1a73e8; }
        .card .sub { font-size: 13px; color: #999; margin-top: 4px; }
        .status-dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 8px; }
        .status-ok { background: #34a853; box-shadow: 0 0 6px rgba(52,168,83,0.4); }
        .status-warn { background: #fbbc04; }
        .status-err { background: #ea4335; }
        .status-stopped { background: #999; }
        .btn { display: inline-block; padding: 8px 16px; border: none; border-radius: 6px; font-size: 13px; cursor: pointer; font-weight: 500; transition: all 0.2s; }
        .btn-primary { background: #1a73e8; color: white; }
        .btn-primary:hover { background: #1557b0; }
        .btn-danger { background: #ea4335; color: white; }
        .btn-danger:hover { background: #c5221f; }
        .btn-success { background: #34a853; color: white; }
        .btn-success:hover { background: #2d8e47; }
        .btn:disabled { opacity: 0.5; cursor: not-allowed; }
        .controls { display: flex; gap: 8px; margin-top: 12px; flex-wrap: wrap; }
        .controls input { padding: 8px 12px; border: 1px solid #ddd; border-radius: 6px; font-size: 13px; width: 120px; }
        .list { max-height: 280px; overflow-y: auto; }
        .list-item { padding: 10px 0; border-bottom: 1px solid #f0f0f0; font-size: 13px; display: flex; justify-content: space-between; align-items: center; }
        .list-item:last-child { border-bottom: none; }
        .tag { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }
        .tag-buy { background: #fce8e6; color: #ea4335; }
        .tag-sell { background: #e6f4ea; color: #34a853; }
        .tag-hold { background: #f1f3f4; color: #666; }
        .tag-executed { background: #e8f0fe; color: #1a73e8; }
        .tag-skipped { background: #fef7e0; color: #f9a825; }
        .tag-blocked { background: #fce8e6; color: #ea4335; }
        .tag-error { background: #3c4043; color: #fff; }
        .empty { color: #999; font-size: 13px; padding: 20px 0; text-align: center; }
        .stats-row { display: flex; gap: 24px; margin-top: 8px; }
        .stat { text-align: center; }
        .stat .num { font-size: 24px; font-weight: 700; }
        .stat .label { font-size: 11px; color: #999; margin-top: 2px; }
        .full-width { grid-column: 1 / -1; }
        .sse-status { font-size: 11px; color: #999; }
    </style>
</head>
<body>
    <div class="header">
        <div>
            <h1>Stock Model Dashboard</h1>
            <div class="version">v3.0 - Pipeline + Real-time</div>
        </div>
        <div class="sse-status" id="sse-status">SSE: 连接中...</div>
    </div>
    <div class="container">
        <div class="grid">
            <!-- Pipeline 状态 -->
            <div class="card">
                <h2>Pipeline 状态</h2>
                <div id="pipeline-status">
                    <span class="status-dot status-stopped"></span>未启动
                </div>
                <div class="stats-row" id="pipeline-stats">
                    <div class="stat"><div class="num" id="stat-runs">0</div><div class="label">总运行</div></div>
                    <div class="stat"><div class="num" id="stat-symbols">0</div><div class="label">监控数</div></div>
                </div>
                <div class="controls">
                    <input type="number" id="interval-input" value="30" min="1" max="1440" placeholder="间隔(分)">
                    <button class="btn btn-primary" id="btn-start" onclick="startPipeline()">启动</button>
                    <button class="btn btn-danger" id="btn-stop" onclick="stopPipeline()" disabled>停止</button>
                    <button class="btn btn-success" onclick="runOnce()">手动执行</button>
                </div>
            </div>
            <!-- 系统健康 -->
            <div class="card">
                <h2>系统状态</h2>
                <div id="health"><span class="status-dot status-ok"></span>加载中...</div>
                <div class="sub" id="health-detail"></div>
            </div>
            <!-- 最近信号 -->
            <div class="card">
                <h2>最近信号</h2>
                <div class="list" id="signals"><div class="empty">暂无信号</div></div>
            </div>
            <!-- 回测结果 -->
            <div class="card">
                <h2>回测结果</h2>
                <div class="list" id="backtest"><div class="empty">暂无回测结果</div></div>
            </div>
            <!-- Pipeline 历史 -->
            <div class="card full-width">
                <h2>Pipeline 运行历史</h2>
                <div class="list" id="pipeline-history"><div class="empty">暂无运行记录</div></div>
            </div>
        </div>
    </div>
    <script>
        const API = '';
        let sseConnected = false;

        // SSE 连接
        function connectSSE() {
            const es = new EventSource(API + '/api/events');
            es.onopen = () => {
                sseConnected = true;
                document.getElementById('sse-status').textContent = 'SSE: 已连接';
                document.getElementById('sse-status').style.color = '#34a853';
            };
            es.onmessage = (e) => {
                try {
                    const msg = JSON.parse(e.data);
                    if (msg.event === 'heartbeat') return;
                    if (msg.event === 'signal') addSignalItem(msg.data);
                    if (msg.event === 'backtest') addBacktestItem(msg.data);
                    if (msg.event === 'pipeline_result') addHistoryItem(msg.data);
                } catch(err) {}
            };
            es.onerror = () => {
                sseConnected = false;
                document.getElementById('sse-status').textContent = 'SSE: 断开, 重连中...';
                document.getElementById('sse-status').style.color = '#ea4335';
                setTimeout(connectSSE, 5000);
            };
        }

        // Pipeline 控制
        async function startPipeline() {
            const interval = parseInt(document.getElementById('interval-input').value) || 30;
            const res = await fetch(API + '/api/pipeline/start', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({interval_minutes: interval})
            });
            const data = await res.json();
            updatePipelineUI();
        }

        async function stopPipeline() {
            await fetch(API + '/api/pipeline/stop', {method: 'POST'});
            updatePipelineUI();
        }

        async function runOnce() {
            const res = await fetch(API + '/api/pipeline/run', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({})
            });
            const data = await res.json();
            loadPipelineHistory();
        }

        async function updatePipelineUI() {
            const res = await fetch(API + '/api/pipeline/status');
            const data = await res.json();
            const statusEl = document.getElementById('pipeline-status');
            const dotClass = data.status === 'running' ? 'status-ok' : 'status-stopped';
            const statusText = data.status === 'running' ? '运行中' : '已停止';
            statusEl.innerHTML = '<span class="status-dot ' + dotClass + '"></span>' + statusText;
            document.getElementById('stat-runs').textContent = data.total_runs || 0;
            document.getElementById('stat-symbols').textContent = (data.watchlist || []).length;
            document.getElementById('btn-start').disabled = data.status === 'running';
            document.getElementById('btn-stop').disabled = data.status !== 'running';
        }

        // 数据加载
        async function loadHealth() {
            const r = await fetch(API + '/api/health');
            const d = await r.json();
            document.getElementById('health').innerHTML =
                '<span class="status-dot status-ok"></span>运行正常 v' + d.version;
            document.getElementById('health-detail').textContent =
                'Pipeline: ' + (d.pipeline_status || 'unknown');
        }

        async function loadSignals() {
            const r = await fetch(API + '/api/signals?limit=15');
            const d = await r.json();
            const el = document.getElementById('signals');
            if (!d.signals || d.signals.length === 0) {
                el.innerHTML = '<div class="empty">暂无信号</div>';
                return;
            }
            el.innerHTML = d.signals.reverse().map(s => {
                const tagClass = 'tag-' + (s.action || 'hold').toLowerCase();
                return '<div class="list-item"><div><span class="tag ' + tagClass + '">' +
                    (s.action || 'HOLD') + '</span> ' + s.symbol +
                    ' <span style="color:#999">' + (s.confidence*100||0).toFixed(0) + '%</span></div>' +
                    '<div style="color:#999;font-size:11px">' + (s.timestamp||'').slice(11,19) + '</div></div>';
            }).join('');
        }

        function addSignalItem(s) {
            const el = document.getElementById('signals');
            const empty = el.querySelector('.empty');
            if (empty) empty.remove();
            const tagClass = 'tag-' + (s.action || 'hold').toLowerCase();
            const item = document.createElement('div');
            item.className = 'list-item';
            item.innerHTML = '<div><span class="tag ' + tagClass + '">' +
                (s.action || 'HOLD') + '</span> ' + s.symbol +
                ' <span style="color:#999">' + (s.confidence*100||0).toFixed(0) + '%</span></div>' +
                '<div style="color:#999;font-size:11px">刚刚</div>';
            el.insertBefore(item, el.firstChild);
        }

        async function loadBacktest() {
            const r = await fetch(API + '/api/backtest/results?limit=10');
            const d = await r.json();
            const el = document.getElementById('backtest');
            if (!d.results || d.results.length === 0) {
                el.innerHTML = '<div class="empty">暂无回测结果</div>';
                return;
            }
            el.innerHTML = d.results.reverse().map(b => {
                const ret = ((b.metrics && b.metrics.total_return) || 0) * 100;
                const color = ret >= 0 ? '#34a853' : '#ea4335';
                return '<div class="list-item"><div>' + b.symbol +
                    ' | <span style="color:' + color + ';font-weight:600">' +
                    ret.toFixed(1) + '%</span></div>' +
                    '<div style="color:#999;font-size:11px">' + (b.timestamp||'').slice(11,19) + '</div></div>';
            }).join('');
        }

        function addBacktestItem(b) {
            const el = document.getElementById('backtest');
            const empty = el.querySelector('.empty');
            if (empty) empty.remove();
            const ret = ((b.metrics && b.metrics.total_return) || 0) * 100;
            const color = ret >= 0 ? '#34a853' : '#ea4335';
            const item = document.createElement('div');
            item.className = 'list-item';
            item.innerHTML = '<div>' + b.symbol +
                ' | <span style="color:' + color + ';font-weight:600">' +
                ret.toFixed(1) + '%</span></div>' +
                '<div style="color:#999;font-size:11px">刚刚</div>';
            el.insertBefore(item, el.firstChild);
        }

        async function loadPipelineHistory() {
            const r = await fetch(API + '/api/pipeline/history?limit=20');
            const d = await r.json();
            const el = document.getElementById('pipeline-history');
            if (!d.history || d.history.length === 0) {
                el.innerHTML = '<div class="empty">暂无运行记录</div>';
                return;
            }
            el.innerHTML = d.history.reverse().map(h => {
                const tagClass = 'tag-' + (h.status || 'executed');
                const action = h.action ? ' | ' + h.action : '';
                return '<div class="list-item"><div><span class="tag ' + tagClass + '">' +
                    (h.status || 'executed') + '</span> ' + (h.symbol || 'batch') +
                    action + ' <span style="color:#999">' +
                    (h.quality_score || 0).toFixed(2) + '</span></div>' +
                    '<div style="color:#999;font-size:11px">' +
                    (h.timestamp || '').slice(11, 19) + '</div></div>';
            }).join('');
        }

        function addHistoryItem(h) {
            const el = document.getElementById('pipeline-history');
            const empty = el.querySelector('.empty');
            if (empty) empty.remove();
            const tagClass = 'tag-' + (h.status || 'executed');
            const action = h.action ? ' | ' + h.action : '';
            const item = document.createElement('div');
            item.className = 'list-item';
            item.innerHTML = '<div><span class="tag ' + tagClass + '">' +
                (h.status || 'executed') + '</span> ' + (h.symbol || 'batch') +
                action + '</div><div style="color:#999;font-size:11px">刚刚</div>';
            el.insertBefore(item, el.firstChild);
        }

        // 初始化
        loadHealth(); loadSignals(); loadBacktest(); loadPipelineHistory(); updatePipelineUI();
        setInterval(loadHealth, 30000);
        setInterval(updatePipelineUI, 10000);
        connectSSE();
    </script>
</body>
</html>"""
