"""模拟盘账户迁移: 从「写死起点爬历史」切换到「滚动窗口」

缺陷(2026-10-21 用户实测发现):
  账户 start_date=20240101 写死, 每交易日推进一根 bar。今天 2026-10-21,
  cursor 才到 2024-04-17 —— 比"今天"慢 **2.4 年**。观察期要攒 20 笔成交
  得等一两年, 且评估的是 2024 年的市场, 对当下没参考价值。

方案 A(用户选定, 含 A1 重置口径):
  1. 取数起点 = 滚动窗口(最近 DEFAULT_LOOKBACK_BARS 根)
  2. 迁移已有账户: 重置成交/持仓/曲线/现金, cursor 归 0, 从窗口第 0 根开跑
     —— 保留旧窗口产生的成交会让"起点持仓"与窗口历史不一致,
     且观察期要的是**同一口径下的连续记录**(用户明确选 A1)

契约:
  - 迁移后 cursor=0, 曲线为空, 无持仓无成交, 现金=初始资金
  - 迁移后 start_date 是窗口起点(YYYYMMDD, 真实交易日)
  - 迁移前必须备份(可回滚)
  - 迁移是**幂等**的: 已是窗口起点的账户再迁移不报错
  - 迁移不清 symbols/data_source(用户配置, 不是历史)
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from stock_model.web.paper_api import DEFAULT_LOOKBACK_BARS, migrate_to_rolling_window

if TYPE_CHECKING:
    from pathlib import Path


def _account(tmp_path: Path, **over) -> Path:
    p = tmp_path / "default.json"
    d = {
        "cash": 8384.98,
        "initial_capital": 10000.0,
        "positions": {"000002": {"symbol": "000002", "shares": 200, "avg_cost": 8.05}},
        "trades": [{"trade_id": "T000001", "symbol": "000002", "side": "buy", "shares": 200}],
        "equity_curve": [
            {"date": "2024-01-03", "cash": 10000.0, "market_value": 0.0, "total_asset": 10000.0},
            {"date": "2024-04-17", "cash": 8384.98, "market_value": 1428.0, "total_asset": 9812.98},
        ],
        "_metadata": {
            "symbols": ["000002"],
            "data_source": "baostock",
            "start_date": "20240101",
            "cursor": {"000002": 68},
            "initial_capital": 10000.0,
        },
    }
    d.update(over)
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return p


def _patch_fetcher(monkeypatch, idx) -> None:
    """让 fetcher 返回给定索引的数据(避免依赖真实数据源)"""
    import pandas as pd

    class FakeFetcher:
        def __init__(self, source=None):
            self.source = source

        def get_daily(self, symbol, start_date=None, end_date=None):
            return pd.DataFrame({"close": [1.0] * len(idx)}, index=idx)

    # _rolling_start_date 内部 `from stock_model.data.fetcher import StockDataFetcher`,
    # 故 patch 源模块属性
    monkeypatch.setattr("stock_model.data.fetcher.StockDataFetcher", FakeFetcher)


class TestMigrateToRollingWindow:
    def test_resets_to_window_start(self, tmp_path, monkeypatch):
        """迁移后: cursor=0 / 曲线空 / 无持仓无成交 / 现金=初始"""
        import pandas as pd

        idx = pd.bdate_range("2025-07-23", periods=400)
        _patch_fetcher(monkeypatch, idx)

        p = _account(tmp_path)
        migrate_to_rolling_window(p)

        d = json.loads(p.read_text(encoding="utf-8"))
        assert d["_metadata"]["cursor"] == {"000002": 0}, "cursor 必须归 0"
        assert d["equity_curve"] == [], "曲线必须清空(旧窗口口径不再适用)"
        assert d["positions"] == {}, "持仓必须清空"
        assert d["trades"] == [], "成交必须清空"
        assert d["cash"] == pytest.approx(10000.0), "现金回初始资金"
        assert d["_metadata"]["start_date"] == str(idx[100].date()).replace("-", "")

    def test_backup_created(self, tmp_path, monkeypatch):
        """迁移前必须备份(可回滚)"""
        import pandas as pd

        idx = pd.bdate_range("2025-07-23", periods=400)
        _patch_fetcher(monkeypatch, idx)
        p = _account(tmp_path)
        before = p.read_text(encoding="utf-8")
        migrate_to_rolling_window(p)
        baks = list(tmp_path.glob("*.bak_*"))
        assert baks, "必须生成备份"
        assert baks[0].read_text(encoding="utf-8") == before, "备份内容须与迁移前一致"

    def test_preserves_user_config(self, tmp_path, monkeypatch):
        """symbols / data_source 是用户配置, 迁移不得动"""
        import pandas as pd

        idx = pd.bdate_range("2025-07-23", periods=400)
        _patch_fetcher(monkeypatch, idx)
        p = _account(tmp_path)
        migrate_to_rolling_window(p)
        d = json.loads(p.read_text(encoding="utf-8"))
        assert d["_metadata"]["symbols"] == ["000002"]
        assert d["_metadata"]["data_source"] == "baostock"

    def test_idempotent(self, tmp_path, monkeypatch):
        """幂等: 已迁移过的账户再迁移不报错、不二次破坏"""
        import pandas as pd

        idx = pd.bdate_range("2025-07-23", periods=400)
        _patch_fetcher(monkeypatch, idx)
        p = _account(tmp_path)
        migrate_to_rolling_window(p)
        first = p.read_text(encoding="utf-8")
        migrate_to_rolling_window(p)  # 再跑一次
        assert p.read_text(encoding="utf-8") == first, "二次迁移应无变化"

    def test_missing_file_raises(self, tmp_path):
        """账户文件不存在要显式报错(不能静默"成功")"""
        missing = tmp_path / "nope.json"
        with pytest.raises(FileNotFoundError, match="账户文件不存在"):
            migrate_to_rolling_window(missing)

    def test_missing_file_leaves_no_backup(self, tmp_path):
        """文件不存在时不得生成任何备份(不能留下误导性文件)"""
        missing = tmp_path / "ghost.json"
        with pytest.raises(FileNotFoundError):
            migrate_to_rolling_window(missing)
        assert list(tmp_path.glob("*.bak_*")) == [], "不该有备份"
        assert not missing.exists(), "不该凭空创建账户文件"


def test_default_lookback_covers_ma250():
    """窗口必须覆盖最长指标 MA250"""
    assert DEFAULT_LOOKBACK_BARS >= 250
