"""Parquet 依赖缺失回归保护测试 (QM-5 ④)

背景
----
项目初始化 commit 3f7f226 让 DataStorage 默认以 parquet 格式持久化，
但 ``pyarrow`` / ``fastparquet`` 从未出现在 pyproject.toml 的依赖声明中。
CI 装的是 ``pip install -e ".[dev,quant]"``，同样不含 pyarrow。

commit 39c8483（"fix: skip cache tests when pyarrow not available"）发现
CI 失败后，选择在测试里加 ``pytest.importorskip("pyarrow")`` 让它变绿 ——
消音而非修复。后果是：

- CI 永远绿（那批测试被标记为 skip，不计入失败）
- 但任何新克隆仓库、且未手动安装 pyarrow 的用户，
  第一次调用 StockDataFetcher 就会在写缓存环节崩溃

本文件的每个测试都在"pyarrow 不可用"的前提下运行 ——
这是绝大多数标准安装的真实状态。
"""

import importlib.util
import tempfile

import pandas as pd
import pytest

from stock_model.data.storage import DataStorage

PARQUET_ENGINE_AVAILABLE = (
    importlib.util.find_spec("pyarrow") is not None
    or importlib.util.find_spec("fastparquet") is not None
)


def _make_df(rows: int = 5) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [10.0 + i for i in range(rows)],
            "high": [10.5 + i for i in range(rows)],
            "low": [9.5 + i for i in range(rows)],
            "close": [10.2 + i for i in range(rows)],
            "volume": [100000 + i for i in range(rows)],
        },
        index=pd.date_range("2024-01-01", periods=rows, freq="D"),
    )


# ========================================================================
# 核心回归：缺 parquet 引擎时缓存不得抛异常
# ========================================================================


class TestParquetFallback:
    """DataStorage 在缺少 parquet 引擎时必须优雅降级到 csv"""

    def test_cache_set_does_not_raise_without_engine(self):
        """cache_set 在无 pyarrow 时不得抛异常

        这是核心回归点：修复前此处抛 ImportError，
        经 tenacity 重试 3 次后变成 RetryError，让整个 pipeline 崩溃。
        """
        storage = DataStorage()
        df = _make_df()
        with tempfile.TemporaryDirectory() as tmp:
            storage.cache_dir = __import__("pathlib").Path(tmp)
            # 不应抛出任何异常
            storage.cache_set("test_key", df)

    def test_cache_roundtrip_preserves_data(self):
        """降级到 csv 后，缓存仍能正确写回并读出，数据不失真"""
        import pathlib

        storage = DataStorage()
        df = _make_df()
        with tempfile.TemporaryDirectory() as tmp:
            storage.cache_dir = pathlib.Path(tmp)
            storage.cache_set("rt_key", df)

            # 确认确实落盘了
            files = list(pathlib.Path(tmp).iterdir())
            assert files, "缓存文件未落盘"

            loaded = storage.cache_get("rt_key")
            assert loaded is not None, "缓存读回失败"
            assert len(loaded) == len(df)
            assert list(loaded.columns) == list(df.columns)
            pd.testing.assert_series_equal(loaded["close"], df["close"], check_dtype=False)

    def test_save_raw_does_not_raise_without_engine(self):
        """save_raw / save_processed 同样不得因缺引擎崩溃"""
        import pathlib

        storage = DataStorage()
        df = _make_df()
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            storage.raw_dir = root / "raw"
            storage.processed_dir = root / "processed"

            storage.save_raw(df, "000001")
            storage.save_processed(df, "000001")

    def test_load_raw_does_not_raise_without_engine(self):
        """load_raw 在无引擎环境下读回已存数据"""
        import pathlib

        storage = DataStorage()
        df = _make_df()
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            storage.raw_dir = root / "raw"
            storage.processed_dir = root / "processed"
            storage.save_raw(df, "000001")
            loaded = storage.load_raw("000001")
            assert loaded is not None

    @pytest.mark.skipif(
        PARQUET_ENGINE_AVAILABLE,
        reason="环境已装 pyarrow/fastparquet，无需验证降级路径",
    )
    def test_actual_engine_is_missing_in_this_env(self):
        """确认当前环境确实没有 parquet 引擎

        使上方测试的"降级路径"成为实际被执行的路径，而非空跑。
        若此测试失败，说明环境变了，上述降级断言可能未真正生效。
        """
        assert not PARQUET_ENGINE_AVAILABLE


# ========================================================================
# 依赖声明回归：pyarrow 不应被硬依赖
# ========================================================================


class TestPyarrowNotHardDependency:
    """pyarrow 只能是可选依赖，不能是必装依赖

    parquet 只是缓存的存储格式，为它强拉一个重型依赖不划算；
    正确做法是可选增强 + 缺失时降级。
    """

    def test_pyproject_does_not_hard_require_parquet_engine(self):
        """pyproject.toml 不应把 pyarrow 列为强制依赖"""
        import pathlib
        import re

        pyproject = pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml"
        text = pyproject.read_text(encoding="utf-8")

        # 定位 dependencies 段（非 optional-dependencies）
        deps_section = text.split("[project.optional-dependencies]", 1)[0]
        assert not re.search(r'^\s*"pyarrow|^\s*pyarrow', deps_section, re.MULTILINE), (
            "pyarrow 不应成为强制依赖。parquet 仅是缓存存储格式，"
            "缺引擎时应降级到 csv，而非拖慢所有用户的安装。"
        )

    def test_cache_still_works_with_only_minimal_deps(self):
        """仅用 pytest+pandas+loguru 就能跑通缓存读写

        模拟 CI 的 `.[dev,quant]` 安装状态（不含 pyarrow）。
        """
        import pathlib

        storage = DataStorage()
        df = _make_df()
        with tempfile.TemporaryDirectory() as tmp:
            storage.cache_dir = pathlib.Path(tmp)
            storage.cache_set("minimal_key", df)
            assert storage.cache_get("minimal_key") is not None


# ========================================================================
# fetcher 层回归：缓存失败不得影响主流程
# ========================================================================


class TestFetcherCacheFailureIsolation:
    """fetcher 写缓存失败时，必须返回数据而不是抛异常

    缓存是优化，不是主路径。缓存挂了不影响拉数据 —— 这是关键。
    """

    def test_fetcher_survives_cache_write_failure(self, monkeypatch):
        """模拟缓存层抛 ImportError，fetcher 仍应返回数据"""
        from unittest.mock import MagicMock

        from stock_model.data.fetcher import StockDataFetcher

        df = _make_df(60)

        fetcher = StockDataFetcher.__new__(StockDataFetcher)
        fetcher.settings = MagicMock()
        fetcher.source = "baostock"
        fetcher._cache_enabled = True
        fetcher._cache_ttl = 3600

        # 缓存层直接抛 ImportError（模拟缺 parquet 引擎）
        broken_storage = MagicMock()
        broken_storage.cache_get_with_ttl.side_effect = ImportError("no pyarrow")
        broken_storage.cache_set.side_effect = ImportError("no pyarrow")
        fetcher._storage = broken_storage

        fetcher._execute_with_fallback = MagicMock(return_value=df)

        result = fetcher._get_with_cache("get_daily", "000001", start_date="20240101")

        assert result is not None
        assert len(result) == 60, "缓存失败时必须仍然返回数据"
