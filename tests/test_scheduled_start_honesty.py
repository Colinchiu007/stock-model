"""定时启动「假成功」回归保护 (QM-5 ④)

问题
----
`AutoDataCollector.start()` 在缺少 apscheduler 时只 log 一条 WARNING，
既不抛异常也不设置失败标记：

```python
try:
    from apscheduler.schedulers.background import BackgroundScheduler
    ...
except ImportError:
    logger.warning("apscheduler 未安装，定时采集不可用。请安装: pip install apscheduler")
```

于是调用链上游全部以为启动成功：

```
collector.start()      -> 静默 no-op, _running 仍为 False
pipeline.start_scheduled() -> 照常打印 "Pipeline定时执行已启动"
web /api/pipeline/start    -> 无条件把状态写成 "running" 并返回 "已启动"
```

用户看到界面显示"运行中"，实际一个任务都不会跑。

实测复现（mock 掉 apscheduler）：

```
pipeline.is_running = False
collector._running  = False
scheduler            = None
```

逃逸原因
--------
`tests/test_trading_pipeline.py` 只有 `test_is_running_default_false`
（验证初始为 False），**没有任何测试验证 `start_scheduled` 之后
`is_running` 变成 True**。而该文件的头注释却写着
"覆盖：定时执行(start/stop/is_running)" —— 承诺的覆盖从未落地。

本文件锁定三层的行为：collector 明确报告成败、pipeline 不谎报、
web 层不再把失败当成功。
"""

import builtins
import sys
from unittest.mock import MagicMock, patch

import pytest

from stock_model.data.collector import AutoDataCollector
from stock_model.pipeline.config import PipelineConfig
from stock_model.pipeline.trading_pipeline import TradingPipeline
from stock_model.strategy.base import ActionType, BaseStrategy, StrategyResult


class _NoopStrategy(BaseStrategy):
    name = "noop"

    def analyze(self, symbol, df):
        return StrategyResult(symbol, ActionType.HOLD, 0.5, "test")

    def evaluate(self, symbol, df):
        return {}


def _block_apscheduler():
    """让 import apscheduler 抛 ImportError，模拟未安装环境"""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("apscheduler"):
            raise ImportError("No module named 'apscheduler'")
        return real_import(name, *args, **kwargs)

    for mod in ("apscheduler", "apscheduler.schedulers", "apscheduler.schedulers.background"):
        sys.modules.pop(mod, None)
    return patch("builtins.__import__", fake_import)


# ========================================================================
# 第一层：collector 必须明确报告成败
# ========================================================================


class TestCollectorStartFailureIsVisible:
    """collector.start 缺依赖时不得静默失败"""

    def test_collector_start_raises_when_scheduler_unavailable(self):
        """缺 apscheduler 时 start() 必须抛异常，而不是静默返回

        调用方需要据此告知用户"启动失败"，不能收到一个"看起来成功"的无声返回。
        """
        collector = AutoDataCollector(fetcher=MagicMock())

        with _block_apscheduler(), pytest.raises(ImportError):
            collector.start(interval_minutes=30)

    def test_collector_not_running_after_failed_start(self):
        """启动失败后 _running 必须仍为 False"""
        collector = AutoDataCollector(fetcher=MagicMock())

        with _block_apscheduler(), pytest.raises(ImportError):
            collector.start(interval_minutes=30)

        assert collector.is_running is False
        assert collector._scheduler is None

    def test_error_message_names_missing_dependency(self):
        """异常信息必须点名缺哪个包，便于用户自助排障"""
        collector = AutoDataCollector(fetcher=MagicMock())

        with _block_apscheduler(), pytest.raises(ImportError) as exc:
            collector.start(interval_minutes=30)

        assert "apscheduler" in str(exc.value).lower(), (
            f"异常信息应说明缺少 apscheduler, 实际: {exc.value}"
        )


# ========================================================================
# 第二层：pipeline 不得谎报已启动
# ========================================================================


class TestPipelineStartDoesNotLie:
    """pipeline.start_scheduled 失败时不得打「已启动」日志"""

    def test_start_scheduled_raises_when_unavailable(self):
        """底层启动失败必须向上传播"""
        pipeline = TradingPipeline(
            PipelineConfig(watchlist=["000001"], data_source="baostock", enable_notify=False)
        )
        pipeline.add_strategy(_NoopStrategy())

        with _block_apscheduler(), pytest.raises(ImportError):
            pipeline.start_scheduled(interval_minutes=30)

    def test_pipeline_not_running_after_failed_start(self):
        """启动失败后 is_running 必须为 False"""
        pipeline = TradingPipeline(
            PipelineConfig(watchlist=["000001"], data_source="baostock", enable_notify=False)
        )
        pipeline.add_strategy(_NoopStrategy())

        with _block_apscheduler(), pytest.raises(ImportError):
            pipeline.start_scheduled(interval_minutes=30)

        assert pipeline.is_running is False, (
            "启动失败后 is_running 仍为 True —— 这会让上层误判为运行中"
        )

    def test_is_running_reflects_real_scheduler_state(self):
        """is_running 必须反映真实调度器状态，而非乐观假设

        这条是根本性质：无论调用路径如何，is_running 为 True
        都必须意味着确实有调度器在跑。
        """
        pipeline = TradingPipeline(
            PipelineConfig(watchlist=["000001"], data_source="baostock", enable_notify=False)
        )
        pipeline.add_strategy(_NoopStrategy())

        with _block_apscheduler(), pytest.raises(ImportError):
            pipeline.start_scheduled(interval_minutes=30)

        # 直接篡改底层状态，验证 is_running 不为 True
        assert pipeline._collector.is_running is False
        assert pipeline._collector._scheduler is None
        assert pipeline.is_running is False


# ========================================================================
# 第三层：web 端点不得把失败当成功返回
# ========================================================================


@pytest.fixture(scope="module")
def client():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from stock_model.web.app import create_app

    return TestClient(create_app())


class TestWebStartEndpointHonesty:
    """/api/pipeline/start 必须在启动失败时报错，而非返回 started"""

    def test_start_endpoint_raises_503_when_scheduler_missing(self, client):
        """缺 apscheduler 时端点必须返回 503，不能返回 started/running

        503 Service Unavailable 语义上比 500 更准确:
        服务本身正常, 只是缺一个可选依赖。
        """
        with _block_apscheduler():
            resp = client.post("/api/pipeline/start", json={"interval_minutes": 30})

        assert resp.status_code != 200, f"启动失败却返回 200: {resp.json()} —— 用户会以为已在运行"
        assert resp.status_code == 503

    def test_status_not_running_after_failed_start(self, client):
        """启动失败后 status 端点必须报告非 running"""
        with _block_apscheduler():
            client.post("/api/pipeline/start", json={"interval_minutes": 30})
            status = client.get("/api/pipeline/status").json()

        assert status["status"] != "running", f"启动失败后状态仍为 running: {status} —— 假成功"

    def test_error_response_mentions_dependency(self, client):
        """错误信息要让用户知道缺什么"""
        with _block_apscheduler():
            resp = client.post("/api/pipeline/start", json={"interval_minutes": 30})

        body = resp.text.lower()
        assert "apscheduler" in body, f"错误响应应说明缺少 apscheduler, 实际: {resp.text[:200]}"
