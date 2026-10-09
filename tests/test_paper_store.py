"""模拟盘账户持久化测试

背景
----
``web/paper_api.py`` 的引擎原本只存在进程内存的 ``_engines`` 字典,
进程重启后持仓/成交/资金曲线全丢。对「手动点几轮」无所谓,
但**定时运行**是致命的 —— 重启一次就可能丢状态甚至重复交易。

``Account`` 早已具备 ``to_json`` / ``from_json``, 但 API 层从未调用。
本模块负责落盘与恢复, 并保证原子写入(不产生半截文件)。

2026-10-10: ``paper_api`` 已完成接线, ``TestPrerequisiteForScheduling``
里那条 skip 改为真断言。
"""

import json

import pytest

from stock_model.paper.models import Account, Order, OrderStatus, Position, Side, Trade
from stock_model.paper.store import (
    delete_account,
    list_accounts,
    load_account,
    load_metadata,
    save_account,
    store_path,
)


@pytest.fixture
def store_dir(tmp_path):
    return tmp_path / "paper"


def make_account(account_id="test") -> Account:
    acc = Account(account_id=account_id, initial_capital=10000.0, cash=9500.0)
    acc.positions["000002"] = Position(
        "000002", shares=100, avg_cost=10.0, last_price=11.0, open_date="2026-01-05"
    )
    acc.trades.append(
        Trade("T1", "000002", Side.BUY, 100, 10.0, 1000.0, commission=5.0, executed_at="2026-01-05")
    )
    acc.pending_orders.append(Order("000001", Side.BUY, 200, status=OrderStatus.PENDING))
    acc.peak_asset = 10100.0
    return acc


class TestRoundTrip:
    def test_save_then_load_preserves_state(self, store_dir):
        """核心: 保存后读回, 资金/持仓/成交/待订单全部一致"""
        save_account(make_account(), store_dir)
        back = load_account("test", store_dir)

        assert back is not None
        assert back.cash == pytest.approx(9500.0)
        assert back.initial_capital == pytest.approx(10000.0)
        assert back.peak_asset == pytest.approx(10100.0)
        assert len(back.trades) == 1
        assert len(back.pending_orders) == 1
        assert back.positions["000002"].shares == 100

    def test_equity_curve_preserved(self, store_dir):
        from stock_model.paper.models import EquityPoint

        acc = make_account()
        acc.equity_curve.append(
            EquityPoint(date="2026-01-05", total_asset=9500.0, cash=9500.0, market_value=0.0)
        )
        save_account(acc, store_dir)
        back = load_account("test", store_dir)
        assert len(back.equity_curve) == 1
        assert back.equity_curve[0].total_asset == pytest.approx(9500.0)

    def test_metadata_not_leaked_into_account(self, store_dir):
        """_metadata 是附加信息, 不应被当成 Account 字段反序列化"""
        save_account(make_account(), store_dir, metadata={"symbols": ["000002"], "note": "x"})
        path = store_path("test", store_dir)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "_metadata" in data
        back = load_account("test", store_dir)
        assert not hasattr(back, "_metadata")


class TestMissingOrCorrupt:
    def test_load_returns_none_when_absent(self, store_dir):
        assert load_account("never-saved", store_dir) is None

    def test_corrupt_file_returns_none(self, store_dir):
        """损坏文件不得抛异常 —— 定时任务不能因一次坏文件而崩溃"""
        store_dir.mkdir(parents=True, exist_ok=True)
        store_path("broken", store_dir).write_text("{ 这不是合法 JSON", encoding="utf-8")
        assert load_account("broken", store_dir) is None

    def test_missing_fields_tolerated(self, store_dir):
        """字段缺失时应尽力恢复, 而不是整体失败"""
        store_dir.mkdir(parents=True, exist_ok=True)
        store_path("partial", store_dir).write_text(
            json.dumps({"account_id": "partial", "cash": 1234.0}), encoding="utf-8"
        )
        back = load_account("partial", store_dir)
        assert back is not None
        assert back.cash == pytest.approx(1234.0)


class TestAtomicWrite:
    def test_no_temp_files_left(self, store_dir):
        """原子写入后不得残留 .tmp 文件"""
        save_account(make_account(), store_dir)
        leftovers = list(store_dir.glob("*.tmp"))
        assert not leftovers, f"残留临时文件: {leftovers}"

    def test_overwrite_keeps_single_file(self, store_dir):
        """重复保存不得产生多份状态文件"""
        for _ in range(3):
            save_account(make_account(), store_dir)
        files = list(store_dir.glob("*.json"))
        assert len(files) == 1, f"应只有一份状态文件, 实际 {files}"

    def test_concurrent_save_no_corruption(self, store_dir):
        """连续写入后文件仍可解析"""
        acc = make_account()
        for i in range(5):
            acc.cash = 9000.0 + i
            save_account(acc, store_dir)
        back = load_account("test", store_dir)
        assert back is not None
        assert back.cash == pytest.approx(9004.0)


class TestListAndDelete:
    def test_list_accounts(self, store_dir):
        assert list_accounts(store_dir) == []
        save_account(make_account("a"), store_dir)
        save_account(make_account("b"), store_dir)
        assert list_accounts(store_dir) == ["a", "b"]

    def test_delete_account(self, store_dir):
        save_account(make_account("gone"), store_dir)
        assert delete_account("gone", store_dir) is True
        assert load_account("gone", store_dir) is None
        assert delete_account("gone", store_dir) is False

    def test_store_path_uses_account_id(self, store_dir):
        assert store_path("abc", store_dir).name == "abc.json"


class TestPrerequisiteForScheduling:
    """这些是「定时跑」的前置条件 —— 锁住它们防止有人又改回内存态"""

    def test_paper_api_imports_store(self):
        """API 层必须使用 store 模块, 否则重启即丢状态

        这条锁此前是 skip 状态, 用来提醒「持久化模块已就绪但尚未接线」。
        2026-10-10 已接线, 故改为真断言 —— **不得再退回 skip**:
        退回 skip 就等于允许「重启丢状态且不报错」再次发生。
        """
        import inspect

        from stock_model.web import paper_api

        src = inspect.getsource(paper_api)
        assert "paper.store" in src, (
            "paper_api 未接入 store —— 定时运行会丢状态且不报错(这条锁就是防这个的)"
        )
        assert "save_account" in src, "paper_api 必须实际调用 save_account, 只 import 不算接线"
        assert "load_account" in src, "paper_api 必须实际调用 load_account, 否则重启无法恢复"

    def test_store_module_exists(self):
        from stock_model.paper import store

        assert hasattr(store, "save_account")
        assert hasattr(store, "load_account")


class TestMetadata:
    """``_metadata``: 账户之外那部分状态(股票池 / 取数区间 / 推进游标)

    恢复引擎时**只能**从这里拿 —— ``Account`` 上并没有 ``symbols`` /
    ``data_source`` / ``start_date``(踩过: 照直觉写 ``account.symbols``
    直接 AttributeError)。
    """

    def test_round_trip(self, store_dir):
        meta = {"symbols": ["000002"], "start_date": "20240101", "cursor": {"000002": 7}}
        save_account(make_account(), store_dir, metadata=meta)
        back = load_metadata("test", store_dir)
        assert back["symbols"] == ["000002"]
        assert back["cursor"] == {"000002": 7}

    def test_missing_file_returns_empty(self, store_dir):
        """没落盘过 → 空字典(调用方回退默认配置), 不抛异常"""
        assert load_metadata("never-saved", store_dir) == {}

    def test_account_file_without_metadata(self, store_dir):
        """老版本落盘的文件没有 _metadata —— 必须容忍, 不能崩"""
        save_account(make_account(), store_dir)
        assert load_metadata("test", store_dir) == {}

    def test_corrupt_file_returns_empty(self, store_dir):
        """坏文件只该让元数据缺失, 不该让账户打不开"""
        store_dir.mkdir(parents=True, exist_ok=True)
        store_path("broken", store_dir).write_text("{ 坏 JSON", encoding="utf-8")
        assert load_metadata("broken", store_dir) == {}

    def test_non_dict_metadata_ignored(self, store_dir):
        """_metadata 被人手改成了数组/字符串 → 忽略并告警, 不炸"""
        store_dir.mkdir(parents=True, exist_ok=True)
        path = store_path("weird", store_dir)
        path.write_text(json.dumps({"account_id": "weird", "_metadata": [1, 2]}), encoding="utf-8")
        assert load_metadata("weird", store_dir) == {}

    def test_metadata_does_not_break_account_load(self, store_dir):
        """带 _metadata 的文件仍能正常恢复账户"""
        save_account(make_account(), store_dir, metadata={"symbols": ["000002"]})
        assert load_account("test", store_dir) is not None


class TestAtomicWriteHelper:
    """定时配置与账户共用同一个原子写入实现"""

    def test_no_temp_left_and_readable(self, tmp_path):
        from stock_model.paper.store import write_json_atomic

        path = tmp_path / "nested" / "cfg.json"
        write_json_atomic(path, {"a": 1})
        assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1}
        assert list(path.parent.glob("*.tmp")) == []

    def test_failure_leaves_no_temp_file(self, tmp_path):
        """写入失败时不得残留临时文件"""
        from stock_model.paper.store import write_json_atomic

        path = tmp_path / "cfg.json"
        with pytest.raises(TypeError):
            write_json_atomic(path, {"bad": object()})
        assert list(tmp_path.glob("*.tmp")) == []
        assert not path.exists()
