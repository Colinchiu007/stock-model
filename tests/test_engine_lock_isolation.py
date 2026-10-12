"""引擎构造不得持锁做慢速 IO —— 修「一个挂起的账户请求阻塞所有操作」

缺陷(2026-10-13 凌晨实测, E2E 抓到的第 3 个真问题):
  隔离环境(空数据目录)首次访问 /api/paper/account → _get_engine 持
  _account_lock 构造引擎 → BaostockSource() login 挂起 21s+ → **同一把锁**
  被 schedule 端点(421 行)也需要 → 前端「开启定时」点了没反应,
  E2E wait_for_function 15s 超时。

根因: _get_engine 用 `with _account_lock(): ... _build_engine()` ——
     把慢速 IO(数据源 login)放在临界区内。RLock 是**每账户**的,
     一个挂起的构造会阻塞该账户的一切请求(连只读的 schedule 状态)。

契约:
1. _get_engine 的锁只保护 _engines 字典读写(双检锁), 慢速构造在锁外
2. 并发首次请求: 只构造一次(不重复建引擎)
3. schedule 等只读端点**不**被慢构造阻塞(不需要引擎的端点不碰锁)
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.importorskip("fastapi")


class TestEngineConstructionLocking:
    def test_slow_construction_does_not_block_lock_holders(self):
        """构造慢(>1s)时, 别的线程拿 _account_lock 不应被阻塞"""
        from stock_model.web import paper_api

        account_id = "slowtest"
        paper_api._engines.pop(account_id, None)

        def slow_build(*a, **k):
            time.sleep(2.0)
            raise RuntimeError("数据源挂起")

        with patch.object(paper_api, "_build_engine", side_effect=slow_build):
            t = threading.Thread(target=lambda: paper_api._get_engine(account_id), daemon=True)
            t.start()
            time.sleep(0.3)  # 让构造线程进入慢速区

            # 主线程尝试拿锁: 必须**立刻**拿到(不被 2s 构造阻塞)
            t0 = time.time()
            lock = paper_api._account_lock(account_id)
            acquired = lock.acquire(timeout=0.5)
            elapsed = time.time() - t0
            if acquired:
                lock.release()
            assert acquired, "慢构造不应阻塞别的锁使用者"
            assert elapsed < 0.5, f"拿锁耗时 {elapsed:.2f}s, 不应被慢构造拖住"

        t.join(timeout=5)
        paper_api._engines.pop(account_id, None)

    def test_concurrent_first_requests_build_once(self):
        """并发首次请求: 引擎只被构造一次(双检锁仍有效)"""
        from stock_model.web import paper_api

        account_id = "conc"
        paper_api._engines.pop(account_id, None)
        built = []

        class FakeEngine:
            pass

        def counting_build(*a, **k):
            time.sleep(0.2)
            built.append(1)
            return FakeEngine()

        with patch.object(paper_api, "_build_engine", side_effect=counting_build):
            with ThreadPoolExecutor(max_workers=5) as ex:
                futs = [ex.submit(paper_api._get_engine, account_id) for _ in range(5)]
                results = [f.result() for f in futs]

        assert len(built) == 1, f"并发下应只构造一次, 实际 {len(built)}"
        assert all(r is results[0] for r in results), "所有请求应拿到同一实例"
        paper_api._engines.pop(account_id, None)
