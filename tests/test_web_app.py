"""Web Dashboard API 测试

覆盖 GET/POST 端点、Pipeline控制、信号管理、回测、组合优化、数据质量检查。
"""

from unittest.mock import MagicMock, PropertyMock, patch

import pytest

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

# FastAPI 是可选依赖
pytest.importorskip("fastapi")


from fastapi.testclient import TestClient  # noqa: E402

from stock_model.web.app import create_app  # noqa: E402


@pytest.fixture
def client():
    """创建测试客户端(每次新建app隔离状态)"""
    app = create_app()
    with TestClient(app) as c:
        yield c


class TestHealthAPI:
    """健康检查API测试"""

    def test_health_check(self, client):
        """GET /api/health 返回ok"""
        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["version"] == "3.0.0"
        assert "pipeline_status" in data
        assert "timestamp" in data


class TestSignalsAPI:
    """信号API测试"""

    def test_get_signals_empty(self, client):
        """GET /api/signals 空列表"""
        resp = client.get("/api/signals")
        assert resp.status_code == 200
        data = resp.json()
        assert "signals" in data
        assert "total" in data

    def test_get_signals_with_limit(self, client):
        """GET /api/signals?limit=1"""
        resp = client.get("/api/signals", params={"limit": 1})
        assert resp.status_code == 200

    def test_add_signal(self, client):
        """POST /api/signals 添加信号"""
        resp = client.post(
            "/api/signals",
            json={
                "symbol": "000001",
                "action": "BUY",
                "confidence": 0.8,
                "reason": "测试信号",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["signal"]["symbol"] == "000001"
        assert data["signal"]["action"] == "BUY"

    def test_add_signal_default_values(self, client):
        """POST /api/signals 使用默认值"""
        resp = client.post("/api/signals", json={"symbol": "600036"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["signal"]["action"] == "HOLD"
        assert data["signal"]["confidence"] == 0.5

    def test_add_and_retrieve_signal(self, client):
        """添加信号后可查询"""
        client.post(
            "/api/signals",
            json={"symbol": "000002", "action": "SELL", "confidence": 0.6},
        )
        resp = client.get("/api/signals")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] > 0


class TestPipelineAPI:
    """Pipeline控制API测试"""

    def test_pipeline_status_initial(self, client):
        """GET /api/pipeline/status 初始状态"""
        resp = client.get("/api/pipeline/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("idle", "stopped")

    def test_pipeline_stop_not_running(self, client):
        """POST /api/pipeline/stop 未运行时停止"""
        resp = client.post("/api/pipeline/stop")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "not_running"

    def test_pipeline_history_empty(self, client):
        """GET /api/pipeline/history 空历史"""
        resp = client.get("/api/pipeline/history")
        assert resp.status_code == 200
        data = resp.json()
        assert "history" in data
        assert "total" in data

    def test_pipeline_history_with_limit(self, client):
        """GET /api/pipeline/history?limit=5"""
        resp = client.get("/api/pipeline/history", params={"limit": 5})
        assert resp.status_code == 200

    def test_pipeline_start_already_running(self, client):
        """POST /api/pipeline/start 状态已running时返回already_running"""
        app = create_app()
        with patch("stock_model.pipeline.trading_pipeline.TradingPipeline") as MockPipeline:
            mock_instance = MagicMock()
            MockPipeline.return_value = mock_instance
            mock_instance.start_scheduled = MagicMock()

            with TestClient(app) as c:
                resp1 = c.post(
                    "/api/pipeline/start",
                    json={"interval_minutes": 30, "watchlist": ["000001"]},
                )
                assert resp1.status_code == 200

                resp2 = c.post(
                    "/api/pipeline/start",
                    json={"interval_minutes": 60},
                )
                assert resp2.status_code == 200
                data = resp2.json()
                assert data["status"] == "already_running"

    def test_pipeline_start_success(self, client):
        """POST /api/pipeline/start 启动成功"""
        with patch("stock_model.pipeline.trading_pipeline.TradingPipeline") as MockPipeline:
            mock_instance = MagicMock()
            MockPipeline.return_value = mock_instance
            mock_instance.start_scheduled = MagicMock()

            resp = client.post(
                "/api/pipeline/start",
                json={"interval_minutes": 60, "watchlist": ["000001", "600036"]},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "started"
            assert "600036" in data["watchlist"]

    def test_pipeline_stop_running(self, client):
        """POST /api/pipeline/stop 停止运行中的Pipeline"""
        app = create_app()
        with patch("stock_model.pipeline.trading_pipeline.TradingPipeline") as MockPipeline:
            mock_instance = MagicMock()
            MockPipeline.return_value = mock_instance
            mock_instance.start_scheduled = MagicMock()
            mock_instance.stop = MagicMock()

            with TestClient(app) as c:
                # 先启动
                c.post(
                    "/api/pipeline/start",
                    json={"interval_minutes": 30},
                )
                # 再停止
                resp = c.post("/api/pipeline/stop")
                assert resp.status_code == 200
                data = resp.json()
                assert data["status"] == "stopped"

    def test_pipeline_status_with_instance(self, client):
        """GET /api/pipeline/status 有pipeline实例时返回stats"""
        app = create_app()
        with patch("stock_model.pipeline.trading_pipeline.TradingPipeline") as MockPipeline:
            mock_instance = MagicMock()
            MockPipeline.return_value = mock_instance
            mock_instance.start_scheduled = MagicMock()
            type(mock_instance).stats = PropertyMock(return_value={"runs": 5})

            with TestClient(app) as c:
                c.post("/api/pipeline/start", json={"interval_minutes": 30})
                resp = c.get("/api/pipeline/status")
                assert resp.status_code == 200
                data = resp.json()
                assert data["status"] == "running"
                assert "stats" in data

    def test_pipeline_run_once_success(self, client):
        """POST /api/pipeline/run 手动执行成功"""
        with patch("stock_model.pipeline.trading_pipeline.TradingPipeline") as MockPipeline:
            mock_instance = MagicMock()
            MockPipeline.return_value = mock_instance

            # 构造mock summary
            mock_result = MagicMock()
            mock_result.symbol = "000001"
            mock_result.status.value = "success"
            mock_result.quality_score = 0.9
            mock_result.reason = "ok"
            mock_result.strategy_result = None
            mock_result.position_advice = None

            mock_summary = MagicMock()
            mock_summary.total = 1
            mock_summary.executed = 1
            mock_summary.skipped = 0
            mock_summary.blocked = 0
            mock_summary.errors = 0
            mock_summary.success_rate = 1.0
            mock_summary.results = [mock_result]

            mock_instance.run_batch.return_value = mock_summary

            resp = client.post(
                "/api/pipeline/run",
                json={"symbols": ["000001"]},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["total"] == 1
            assert data["executed"] == 1


class TestBacktestAPI:
    """回测API测试"""

    def test_get_backtest_results_empty(self, client):
        """GET /api/backtest/results 空结果"""
        resp = client.get("/api/backtest/results")
        assert resp.status_code == 200
        data = resp.json()
        assert "results" in data
        assert "total" in data

    def test_get_backtest_results_with_limit(self, client):
        """GET /api/backtest/results?limit=5"""
        resp = client.get("/api/backtest/results", params={"limit": 5})
        assert resp.status_code == 200

    def test_run_backtest_no_data(self, client):
        """POST /api/backtest 数据获取失败返回404"""
        with patch("stock_model.data.fetcher.StockDataFetcher") as MockFetcher:
            mock_fetcher = MagicMock()
            MockFetcher.return_value = mock_fetcher
            mock_fetcher.get_daily.return_value = None

            resp = client.post(
                "/api/backtest",
                json={"symbol": "000001"},
            )
            assert resp.status_code == 404

    def test_run_backtest_empty_df(self, client):
        """POST /api/backtest 空DataFrame返回404"""
        import pandas as pd

        with patch("stock_model.data.fetcher.StockDataFetcher") as MockFetcher:
            mock_fetcher = MagicMock()
            MockFetcher.return_value = mock_fetcher
            mock_fetcher.get_daily.return_value = pd.DataFrame()

            resp = client.post(
                "/api/backtest",
                json={"symbol": "000001"},
            )
            assert resp.status_code == 404

    def test_run_backtest_success(self, client):
        """POST /api/backtest 回测成功"""
        import numpy as np
        import pandas as pd

        # 构造测试数据
        dates = pd.date_range("2024-01-01", periods=100, freq="D")
        df = pd.DataFrame(
            {
                "open": np.random.uniform(10, 20, 100),
                "high": np.random.uniform(15, 25, 100),
                "low": np.random.uniform(5, 15, 100),
                "close": np.random.uniform(10, 20, 100),
                "volume": np.random.randint(1000, 10000, 100),
            },
            index=dates,
        )

        with patch("stock_model.data.fetcher.StockDataFetcher") as MockFetcher:
            mock_fetcher = MagicMock()
            MockFetcher.return_value = mock_fetcher
            mock_fetcher.get_daily.return_value = df

            with patch("stock_model.strategy.engine.BacktestEngine") as MockEngine:
                mock_engine_instance = MagicMock()
                MockEngine.return_value = mock_engine_instance

                mock_result = MagicMock()
                mock_result.initial_cash = 100000.0
                mock_result.final_cash = 105000.0
                mock_result.metrics = {"total_return": 0.05}
                mock_result.trades = []
                mock_engine_instance.run.return_value = mock_result

                resp = client.post(
                    "/api/backtest",
                    json={"symbol": "000001", "initial_cash": 100000},
                )
                assert resp.status_code == 200
                data = resp.json()
                assert data["symbol"] == "000001"
                assert "metrics" in data


class TestPortfolioAPI:
    """组合优化API测试"""

    def test_optimize_portfolio_no_data(self, client):
        """GET /api/portfolio/optimize 无数据返回404"""
        with patch("stock_model.data.fetcher.StockDataFetcher") as MockFetcher:
            mock_fetcher = MagicMock()
            MockFetcher.return_value = mock_fetcher
            mock_fetcher.get_daily.return_value = None

            resp = client.get("/api/portfolio/optimize")
            assert resp.status_code == 404

    def test_optimize_portfolio_success(self, client):
        """GET /api/portfolio/optimize 成功"""
        import numpy as np
        import pandas as pd

        dates = pd.date_range("2024-01-01", periods=50, freq="D")
        df = pd.DataFrame(
            {
                "close": np.random.uniform(10, 20, 50),
            },
            index=dates,
        )

        with patch("stock_model.data.fetcher.StockDataFetcher") as MockFetcher:
            mock_fetcher = MagicMock()
            MockFetcher.return_value = mock_fetcher
            mock_fetcher.get_daily.return_value = df

            with patch("stock_model.portfolio.optimizer.PortfolioOptimizer") as MockOptimizer:
                mock_opt = MagicMock()
                MockOptimizer.return_value = mock_opt

                mock_weight = MagicMock()
                mock_weight.symbol = "000001"
                mock_weight.weight = 1.0
                mock_portfolio = MagicMock()
                mock_portfolio.weights = [mock_weight]
                mock_portfolio.total_value = 100000
                mock_opt.equal_weight.return_value = mock_portfolio

                resp = client.get(
                    "/api/portfolio/optimize",
                    params={"symbols": "000001", "method": "equal_weight"},
                )
                assert resp.status_code == 200
                data = resp.json()
                assert data["method"] == "equal_weight"
                assert "weights" in data


class TestQualityAPI:
    """数据质量检查API测试"""

    def test_quality_no_data(self, client):
        """GET /api/quality/{symbol} 无数据返回404"""
        with patch("stock_model.data.fetcher.StockDataFetcher") as MockFetcher:
            mock_fetcher = MagicMock()
            MockFetcher.return_value = mock_fetcher
            mock_fetcher.get_daily.return_value = None

            resp = client.get("/api/quality/000001")
            assert resp.status_code == 404

    def test_quality_empty_df(self, client):
        """GET /api/quality/{symbol} 空DataFrame返回404"""
        import pandas as pd

        with patch("stock_model.data.fetcher.StockDataFetcher") as MockFetcher:
            mock_fetcher = MagicMock()
            MockFetcher.return_value = mock_fetcher
            mock_fetcher.get_daily.return_value = pd.DataFrame()

            resp = client.get("/api/quality/000001")
            assert resp.status_code == 404

    def test_quality_success(self, client):
        """GET /api/quality/{symbol} 成功"""
        import numpy as np
        import pandas as pd

        dates = pd.date_range("2024-01-01", periods=50, freq="D")
        df = pd.DataFrame(
            {
                "close": np.random.uniform(10, 20, 50),
                "volume": np.random.randint(1000, 10000, 50),
            },
            index=dates,
        )

        with patch("stock_model.data.fetcher.StockDataFetcher") as MockFetcher:
            mock_fetcher = MagicMock()
            MockFetcher.return_value = mock_fetcher
            mock_fetcher.get_daily.return_value = df

            with patch("stock_model.data.monitor.DataQualityMonitor") as MockMonitor:
                mock_monitor = MagicMock()
                MockMonitor.return_value = mock_monitor

                mock_report = MagicMock()
                mock_report.score = 0.95
                mock_report.issues = []
                mock_monitor.check.return_value = mock_report

                resp = client.get("/api/quality/000001")
                assert resp.status_code == 200
                data = resp.json()
                assert data["symbol"] == "000001"
                assert data["score"] == 0.95


class TestDashboard:
    """仪表盘测试"""

    def test_dashboard_page(self, client):
        """GET / 返回HTML"""
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")


class TestCreateApp:
    """应用创建测试"""

    def test_create_app(self):
        """create_app返回FastAPI实例"""
        app = create_app()
        assert app is not None
        assert app.title == "Stock Model Dashboard"

    def test_create_app_with_config(self):
        """create_app带配置参数"""
        app = create_app(config={"test": True})
        assert app is not None
