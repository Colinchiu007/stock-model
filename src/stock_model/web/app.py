"""
Web Dashboard 应用

基于 FastAPI 的 Web 仪表盘，提供策略监控、信号推送、组合分析等可视化界面。
依赖 fastapi 和 uvicorn (可选，未安装时降级)。
"""

from __future__ import annotations

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
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import HTMLResponse
        from pydantic import BaseModel

        app = FastAPI(
            title="Stock Model Dashboard",
            description="股票分析与投资模型 - Web Dashboard",
            version="2.0.0",
        )

        # ---- 数据模型 ----

        class SignalRequest(BaseModel):
            symbol: str
            action: str = "HOLD"
            confidence: float = 0.5
            reason: str = ""

        class BacktestRequest(BaseModel):
            symbol: str
            strategy_name: str = "ma_cross"
            start_date: str | None = None
            end_date: str | None = None
            initial_cash: float = 100000.0

        # ---- 状态存储(内存, 生产环境应使用数据库) ----

        _signals: list[dict] = []
        _backtest_results: list[dict] = []

        # ---- API路由 ----

        @app.get("/", response_class=HTMLResponse)
        async def dashboard():
            """仪表盘首页"""
            return _render_dashboard()

        @app.get("/api/health")
        async def health_check():
            """健康检查"""
            return {"status": "ok", "version": "2.0.0", "timestamp": datetime.now().isoformat()}

        @app.get("/api/signals")
        async def get_signals(limit: int = 20):
            """获取最近信号"""
            return {"signals": _signals[-limit:]}

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
            return {"status": "ok", "signal": signal}

        @app.post("/api/backtest")
        async def run_backtest(req: BacktestRequest):
            """运行回测"""
            try:
                from stock_model.data.fetcher import DataFetcher
                from stock_model.strategy.engine import BacktestEngine
                from stock_model.strategy.momentum import MomentumStrategy

                fetcher = DataFetcher()
                df = fetcher.get_daily(req.symbol)
                if df is None or df.empty:
                    raise HTTPException(status_code=404, detail=f"无法获取 {req.symbol} 数据")

                strategy = MomentumStrategy()
                engine = BacktestEngine(initial_cash=req.initial_cash)
                result = engine.run(strategy, df, req.symbol)

                result_dict = {
                    "symbol": req.symbol,
                    "strategy": req.strategy_name,
                    "metrics": result.metrics,
                    "trades_count": len(result.trades),
                    "timestamp": datetime.now().isoformat(),
                }
                _backtest_results.append(result_dict)
                return result_dict

            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"回测失败: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        @app.get("/api/backtest/results")
        async def get_backtest_results(limit: int = 10):
            """获取回测结果"""
            return {"results": _backtest_results[-limit:]}

        @app.get("/api/portfolio/optimize")
        async def optimize_portfolio(symbols: str = "000001,000002", method: str = "equal_weight"):
            """组合优化"""
            try:
                from stock_model.data.fetcher import DataFetcher
                from stock_model.portfolio.optimizer import PortfolioOptimizer

                symbol_list = [s.strip() for s in symbols.split(",")]
                fetcher = DataFetcher()
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
                        returns=returns_dict, prices=prices_dict, total_value=100000
                    )
                else:
                    portfolio = opt_func(
                        symbols=symbol_list, prices=prices_dict, total_value=100000
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
                raise HTTPException(status_code=500, detail=str(e))

        @app.get("/api/quality/{symbol}")
        async def check_data_quality(symbol: str):
            """检查数据质量"""
            try:
                from stock_model.data.fetcher import DataFetcher
                from stock_model.data.monitor import DataQualityMonitor

                fetcher = DataFetcher()
                df = fetcher.get_daily(symbol)
                if df is None or df.empty:
                    raise HTTPException(status_code=404, detail=f"无法获取 {symbol} 数据")

                monitor = DataQualityMonitor()
                report = monitor.check(df, symbol)
                return {"symbol": symbol, "quality": report}

            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"数据质量检查失败: {e}")
                raise HTTPException(status_code=500, detail=str(e))

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
        body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }
        .header { background: #1a73e8; color: white; padding: 20px; border-radius: 8px; margin-bottom: 20px; }
        .header h1 { margin: 0; font-size: 24px; }
        .header p { margin: 5px 0 0; opacity: 0.8; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 16px; }
        .card { background: white; border-radius: 8px; padding: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
        .card h2 { margin: 0 0 12px; font-size: 16px; color: #333; }
        .card .value { font-size: 28px; font-weight: bold; color: #1a73e8; }
        .status { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
        .status-ok { background: #34a853; }
        .status-warn { background: #fbbc04; }
        .status-err { background: #ea4335; }
        #signals { max-height: 300px; overflow-y: auto; }
        .signal-item { padding: 8px; border-bottom: 1px solid #eee; font-size: 13px; }
        .signal-item:last-child { border-bottom: none; }
        .buy { color: #ea4335; } .sell { color: #34a853; } .hold { color: #666; }
    </style>
</head>
<body>
    <div class="header">
        <h1>Stock Model Dashboard</h1>
        <p>股票分析与投资模型 v2.0</p>
    </div>
    <div class="grid">
        <div class="card">
            <h2>系统状态</h2>
            <div id="health"><span class="status status-ok"></span>加载中...</div>
        </div>
        <div class="card">
            <h2>最近信号</h2>
            <div id="signals">加载中...</div>
        </div>
        <div class="card">
            <h2>回测结果</h2>
            <div id="backtest">加载中...</div>
        </div>
    </div>
    <script>
        async function loadHealth() {
            const r = await fetch('/api/health');
            const d = await r.json();
            document.getElementById('health').innerHTML =
                '<span class="status status-ok"></span>运行正常 v' + d.version;
        }
        async function loadSignals() {
            const r = await fetch('/api/signals?limit=10');
            const d = await r.json();
            const html = d.signals.reverse().map(s =>
                '<div class="signal-item"><span class="' + s.action.toLowerCase() + '">' +
                s.action + '</span> ' + s.symbol + ' (' + (s.confidence*100).toFixed(0) +
                '%) ' + s.reason + '</div>'
            ).join('');
            document.getElementById('signals').innerHTML = html || '暂无信号';
        }
        async function loadBacktest() {
            const r = await fetch('/api/backtest/results?limit=5');
            const d = await r.json();
            const html = d.results.reverse().map(b =>
                '<div class="signal-item">' + b.symbol + ' | 收益率: ' +
                ((b.metrics.total_return||0)*100).toFixed(1) + '% | 夏普: ' +
                (b.metrics.sharpe_ratio||0).toFixed(2) + '</div>'
            ).join('');
            document.getElementById('backtest').innerHTML = html || '暂无回测结果';
        }
        loadHealth(); loadSignals(); loadBacktest();
        setInterval(loadHealth, 30000); setInterval(loadSignals, 5000);
    </script>
</body>
</html>"""
