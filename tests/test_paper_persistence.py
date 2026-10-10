"""模拟盘持久化接线测试(重启不丢状态)

为什么单独一个文件
------------------
``tests/test_paper_store.py`` 测的是 store 模块**自己**能不能正确存取。
这里测的是另一件事: **API 层有没有真的用它**, 以及恢复之后引擎能不能
接着往前走。

后者是本项目最怕的那类缺陷: 「看着正常但实际是错的」。具体两个坑:

1. **只恢复账户、不恢复推进游标** → 重启后 ``step()`` 从数据区间开头重新
   推进, 在已经交易过的日期上再交易一遍。界面上的成交记录**变多了**,
   不是空了 —— 比丢状态更难发现。
2. **先建空账户、再把 ``engine.account`` 换成恢复出来的对象** → ``Broker``
   仍绑在空账户上, 之后所有成交都记到"影子账户", 用户看到的账户永远不动。

两条都用真断言锁住, 不靠注释承诺。
"""

from __future__ import annotations

import pandas as pd
import pytest

from stock_model.paper import store
from stock_model.paper.models import Order, Side, Trade
from stock_model.web import paper_api


def _make_df(symbol: str, rows: int = 120, start: str = "2024-01-02", base: float = 10.0):
    """合成日线: 行数够 min_data_rows, 日期是连续工作日(便于验证"往前推进")"""
    idx = pd.bdate_range(start, periods=rows)
    close = [base + i * 0.05 for i in range(rows)]
    return pd.DataFrame(
        {
            "open": close,
            "high": [c + 0.2 for c in close],
            "low": [c - 0.2 for c in close],
            "close": close,
            "volume": [1_000_000.0] * rows,
            "amount": [10_000_000.0] * rows,
            "turnover": [1.0] * rows,
            "pct_change": [0.0] * rows,  # 不涨停也不跌停, 撮合不会被规则挡掉
            "symbol": [symbol] * rows,
        },
        index=idx,
    )


class _FakeFetcher:
    """离线数据源: 测试绝不联网(联网的测试在 CI 上必然 flaky)"""

    rows = 120

    def __init__(self, source: str | None = None) -> None:
        self.source = source

    def get_daily(self, symbol, start_date=None, end_date=None):
        return _make_df(symbol, rows=self.rows)


@pytest.fixture
def paper_env(tmp_path, monkeypatch):
    """隔离环境: 落盘目录指向 tmp, 数据源换成假的, 清空进程内引擎表"""
    store_dir = tmp_path / "paper"
    monkeypatch.setattr(store, "DEFAULT_STORE_DIR", store_dir)
    monkeypatch.setattr("stock_model.data.fetcher.StockDataFetcher", _FakeFetcher)

    paper_api._engines.clear()
    paper_api._locks.clear()
    yield store_dir
    paper_api._engines.clear()
    paper_api._locks.clear()


def _restart() -> None:
    """模拟进程重启: 内存里的引擎全没了, 磁盘还在"""
    paper_api._engines.clear()


# ==================== 引擎自带的进度状态 ====================


class TestEngineState:
    def test_state_dict_carries_pool_and_cursor(self, paper_env):
        engine = paper_api._get_engine("acc")
        engine.step()
        state = engine.state_dict()

        assert state["symbols"] == engine.symbols
        assert state["start_date"] == engine.start_date
        assert state["cursor"], "游标必须落盘, 否则重启会重放历史"

    def test_load_state_restores_cursor(self, paper_env):
        engine = paper_api._get_engine("acc")
        engine.step()
        saved = engine.state_dict()

        fresh = paper_api._build_engine("acc", saved, None)
        assert fresh._cursor == {}, "新引擎默认从零开始"
        fresh.load_state(saved)
        assert fresh._cursor == saved["cursor"]

    def test_load_state_none_is_noop(self, paper_env):
        engine = paper_api._get_engine("acc")
        engine.load_state(None)  # 不抛异常
        assert engine._cursor == {}

    def test_empty_pool_rejected_when_validation_skipped(self):
        """恢复路径跳过取数校验, 但不能连"空池"都放过"""
        from stock_model.paper.engine import PaperEngine

        with pytest.raises(ValueError, match="股票池为空"):
            PaperEngine(symbols=[], validate_symbols=False)


# ==================== 重启后接着跑(核心) ====================


class TestRestartResumes:
    def test_restart_does_not_replay_history(self, paper_env):
        """重启后必须从上次的位置继续, 不能回到数据区间开头重走一遍

        变异验证方式: 注释掉 ``engine.load_state(metadata)`` 这行, 本测试必红
        (会看到 step 日期倒退 + 资金曲线出现重复日期)。
        """
        engine = paper_api._get_engine("acc")
        first_dates = [engine.step()["date"] for _ in range(5)]
        # _persist 返回 (是否成功, 失败原因) —— 不是裸 bool
        persisted, persist_error = paper_api._persist("acc", engine)
        assert persisted is True, persist_error
        n_equity = len(engine.account.equity_curve)

        _restart()
        restored = paper_api._get_engine("acc")

        assert restored._cursor, "重启后游标必须被恢复 —— 否则会重放历史"
        nxt = restored.step()

        assert nxt["date"] > first_dates[-1], (
            f"重启后 step 回到了 {nxt['date']}, 而上轮已推进到 {first_dates[-1]} —— 历史被重放"
        )
        dates = [p.date for p in restored.account.equity_curve]
        assert len(dates) == len(set(dates)), f"资金曲线出现重复日期: {dates}"
        assert len(dates) == n_equity + 1

    def test_account_state_survives_restart(self, paper_env):
        engine = paper_api._get_engine("acc")
        for _ in range(3):
            engine.step()
        engine.account.trades.append(
            Trade(
                "T-1",
                "000002",
                Side.BUY,
                100,
                10.0,
                1000.0,
                commission=5.0,
                executed_at="2024-02-01",
            )
        )
        before = engine.account
        persisted, persist_error = paper_api._persist("acc", engine)
        assert persisted is True, persist_error

        _restart()
        restored = paper_api._get_engine("acc")

        assert restored.account.cash == pytest.approx(before.cash)
        assert restored.account.total_asset == pytest.approx(before.total_asset)
        assert len(restored.account.trades) == 1
        assert restored.account.trades[0].trade_id == "T-1"
        assert len(restored.account.equity_curve) == len(before.equity_curve)
        assert restored.symbols == engine.symbols
        assert restored.start_date == engine.start_date

    def test_broker_binds_to_restored_account(self, paper_env):
        """Broker 必须绑在**恢复出来的**账户上

        若实现是「先建空账户再把 engine.account 换掉」, broker.account 仍指向
        被丢弃的空账户 —— 之后成交都记在影子账户上, 而界面上账户纹丝不动。
        """
        engine = paper_api._get_engine("acc")
        engine.step()
        paper_api._persist("acc", engine)

        _restart()
        restored = paper_api._get_engine("acc")

        assert restored.broker.account is restored.account, (
            "broker.account 与 engine.account 不是同一个对象 —— 成交会记到影子账户上"
        )

        restored.broker.submit_order(Order(symbol="000002", side=Side.BUY, shares=100))
        assert len(restored.account.pending_orders) == 1, "经 broker 下的单没进账户"

    def test_account_id_follows_api_parameter(self, paper_env):
        """账户 id 必须跟随 API 参数

        引擎曾把 account_id 硬编码成 ``paper-default``, 而落盘按 API 的
        account_id 命名 → 多账户互相覆盖。
        """
        engine = paper_api._get_engine("acc-x")
        assert engine.account.account_id == "acc-x"
        engine.step()
        paper_api._persist("acc-x", engine)

        listed = store.list_accounts(paper_env)
        assert "acc-x" in listed, f"落盘文件名没跟上账户 id: {listed}"


# ==================== 落盘时机 ====================


class TestPersistHooks:
    def test_step_persists_via_cycle(self, paper_env):
        result = paper_api.run_paper_cycle("acc", days=3)
        assert result["status"] == "ok"
        assert result["steps"] == 3
        assert result["persisted"] is True
        assert store.store_path("acc", paper_env).exists()

    def test_cycle_creates_state_from_nothing(self, paper_env):
        """第一次跑就该落盘 —— 否则"跑了一周才发现从没存过\""""
        assert store.list_accounts(paper_env) == []
        paper_api.run_paper_cycle("acc", days=1)
        assert store.list_accounts(paper_env) == ["acc"]

    def test_cycle_twice_only_advances(self, paper_env):
        """连跑两次是往前走, 不是把同一段历史跑两遍"""
        r1 = paper_api.run_paper_cycle("acc", days=3)
        r2 = paper_api.run_paper_cycle("acc", days=3)
        assert r2["from"] > r1["to"], f"第二轮从 {r2['from']} 开始, 但第一轮已到 {r1['to']}"

        engine = paper_api._get_engine("acc")
        dates = [p.date for p in engine.account.equity_curve]
        assert len(dates) == len(set(dates))

    def test_persist_failure_is_reported_not_swallowed(self, paper_env, monkeypatch):
        """落盘失败必须显式暴露

        定时场景下"跑成功但没存下来"等同于失败: 下一轮从旧状态继续,
        用户看到的是"每天都在跑, 账却不动"。
        """

        def boom(*a, **k):
            raise OSError("磁盘满了")

        monkeypatch.setattr(store, "write_json_atomic", boom)
        result = paper_api.run_paper_cycle("acc", days=1)

        assert result["persisted"] is False, "落盘失败却报告 persisted=True"
        assert "磁盘满了" in result["persist_error"]

    def test_reset_removes_state_file(self, paper_env):
        """重置必须清磁盘 —— 否则重启后旧账户"复活\""""
        paper_api.run_paper_cycle("acc", days=2)
        assert store.store_path("acc", paper_env).exists()

        from stock_model.paper.store import delete_account

        assert delete_account("acc", paper_env) is True
        assert not store.store_path("acc", paper_env).exists()


# ==================== 恢复路径不在构造时联网 ====================


class TestRestoreDoesNotRefetch:
    def test_restore_skips_symbol_validation(self, paper_env, monkeypatch):
        """恢复时必须跳过逐只取数校验

        否则: 一次网络抖动 = 账户打不开, 而且每次启动都白跑一遍全量取数。
        """
        engine = paper_api._get_engine("acc")
        engine.step()
        paper_api._persist("acc", engine)

        _restart()

        calls = {"n": 0}
        original = _FakeFetcher.get_daily

        def counting(self, *a, **k):
            calls["n"] += 1
            return original(self, *a, **k)

        monkeypatch.setattr(_FakeFetcher, "get_daily", counting)
        restored = paper_api._get_engine("acc")

        assert calls["n"] == 0, f"恢复路径仍在取数({calls['n']} 次) —— 启动会被网络卡住"
        assert len(restored.account.equity_curve) == len(engine.account.equity_curve)
