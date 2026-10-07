"""多 worker 守卫回归保护 (TD-01 收口)

背景
----
本应用的 pipeline 状态、信号历史、SSE 订阅者都保存在**进程内**,
且 ``TradingPipeline`` 持有真实的 APScheduler 后台线程。
多 worker 下各进程互不可见:

    worker-1: 调度器在跑, status=running, 持有 SSE 订阅者
    worker-2: 什么都没,   status=stopped, 无订阅者

同一界面刷新到不同 worker 就显示不同状态, 且信号永远推不到前端。

为什么不用 Redis「共享状态」
--------------------------
半吊子共享比现状更危险: 把 ``status`` 放进 Redis 后, 点「停止」只改到
Redis —— **持有调度器的 worker 仍在跑**, 而面板显示"已停止"。
即「界面说停了, 实际没停」, 比状态不一致严重得多。

正确解法是把 pipeline 抽为独立单例服务(web 做无状态网关),
属架构级改造, 不在当前范围。

因此本次收口方式: **明确拒绝多 worker 启动**,
把「未定义行为」变成「启动即报错」, 好过让使用者踩坑。
"""

import os
import sys
from unittest.mock import patch

import pytest

from stock_model.web.app import (
    MultiWorkerNotSupportedError,
    _assert_single_worker,
    _detect_worker_count,
    create_app,
)


@pytest.fixture
def _clean_env():
    """清除会影响 worker 探测的环境变量"""
    with patch.dict(os.environ, {}, clear=False):
        for key in ("WEB_CONCURRENCY", "GUNICORN_CMD_ARGS"):
            os.environ.pop(key, None)
        yield


class TestDetectWorkerCount:
    """worker 数量探测"""

    def test_default_is_one(self, _clean_env):
        """无任何配置时视为单 worker"""
        with patch.object(sys, "argv", ["uvicorn", "app:create_app"]):
            assert _detect_worker_count() == 1

    def test_uvicorn_workers_flag_space(self):
        """uvicorn --workers 4"""
        with patch.object(sys, "argv", ["uvicorn", "app:create_app", "--workers", "4"]):
            assert _detect_worker_count() == 4

    def test_uvicorn_workers_flag_equals(self):
        """uvicorn --workers=8"""
        with patch.object(sys, "argv", ["uvicorn", "app:create_app", "--workers=8"]):
            assert _detect_worker_count() == 8

    def test_uvicorn_workers_one_is_allowed(self):
        """--workers 1 是合法的单 worker 配置, 不能误伤"""
        with patch.object(sys, "argv", ["uvicorn", "app:create_app", "--workers", "1"]):
            assert _detect_worker_count() == 1

    def test_web_concurrency_env(self):
        """gunicorn WEB_CONCURRENCY=4"""
        with patch.dict(os.environ, {"WEB_CONCURRENCY": "4"}):
            assert _detect_worker_count() == 4

    def test_web_concurrency_one_is_allowed(self):
        """WEB_CONCURRENCY=1 是单 worker, 必须放行 —— 这是最容易写错的边界"""
        with patch.dict(os.environ, {"WEB_CONCURRENCY": "1"}):
            assert _detect_worker_count() == 1

    def test_gunicorn_cmd_args(self):
        """gunicorn GUNICORN_CMD_ARGS='--workers=6'"""
        with patch.dict(os.environ, {"GUNICORN_CMD_ARGS": "--workers=6"}):
            assert _detect_worker_count() == 6

    def test_gunicorn_short_flag(self):
        """gunicorn -w 4 (短参数 + 位置值)"""
        with patch.object(sys, "argv", ["gunicorn", "app:create_app", "-w", "4"]):
            assert _detect_worker_count() == 4

    def test_invalid_value_falls_back_to_one(self, _clean_env):
        """无法解析的值放行, 不因识别失败而误伤正常启动"""
        with patch.dict(os.environ, {"WEB_CONCURRENCY": "abc"}):
            with patch.object(sys, "argv", ["uvicorn", "app:create_app"]):
                assert _detect_worker_count() == 1


class TestAssertSingleWorker:
    """守卫行为"""

    def test_raises_on_multi_worker_argv(self):
        with patch.object(sys, "argv", ["uvicorn", "app:create_app", "--workers", "4"]):
            with pytest.raises(MultiWorkerNotSupportedError) as exc:
                _assert_single_worker()

        msg = str(exc.value)
        assert "4" in msg
        assert "单 worker" in msg, "错误信息应给出可操作的解决方案"
        assert "TD-01" in msg, "错误信息应指向技术债编号, 便于追溯"

    def test_raises_on_web_concurrency(self):
        with patch.dict(os.environ, {"WEB_CONCURRENCY": "8"}):
            with pytest.raises(MultiWorkerNotSupportedError):
                _assert_single_worker()

    def test_passes_on_single_worker(self):
        """单 worker 必须放行, 守卫不能影响正常使用"""
        with patch.dict(os.environ, {"WEB_CONCURRENCY": "1"}):
            with patch.object(sys, "argv", ["uvicorn", "app:create_app"]):
                _assert_single_worker()  # 不应抛异常

    def test_error_is_runtime_error_subclass(self):
        """继承 RuntimeError, 便于上层统一捕获"""
        assert issubclass(MultiWorkerNotSupportedError, RuntimeError)


class TestCreateAppGuard:
    """create_app 必须在多 worker 下拒绝启动"""

    def test_create_app_raises_on_multi_worker(self):
        with patch.object(sys, "argv", ["uvicorn", "app:create_app", "--workers", "4"]):
            with pytest.raises(MultiWorkerNotSupportedError):
                create_app()

    def test_create_app_works_on_single_worker(self, _clean_env):
        """单 worker 正常创建应用"""
        pytest.importorskip("fastapi")
        with patch.object(sys, "argv", ["uvicorn", "app:create_app"]):
            app = create_app()
        assert app is not None

    def test_guard_runs_before_app_creation(self):
        """守卫必须早于 FastAPI 实例创建 —— 否则多 worker 下已产生副作用"""
        with patch.object(sys, "argv", ["uvicorn", "app:create_app", "--workers", "4"]):
            with patch("fastapi.FastAPI") as mock_fastapi:
                with pytest.raises(MultiWorkerNotSupportedError):
                    create_app()
                mock_fastapi.assert_not_called()
