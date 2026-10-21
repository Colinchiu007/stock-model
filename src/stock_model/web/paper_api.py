"""模拟盘 API 路由

端点清单见 ``docs/phase4_prd_paper_trading.md``。

**诚实性约定**
--------------
每个响应都带 ``disclaimer`` 字段明确标注「模拟盘 · 非真实交易」,
且 ``metrics.reliable=False`` 时 ``warnings`` 必非空 —— 前端须显著展示,
不得只挑好看的数字。

**状态持久化**
--------------
引擎不再只存在内存里: 每次推进后账户与**推进游标**一起落盘
(``paper/store.py``), 进程重启后按 ``account_id`` 恢复。
定时运行必须依赖它 —— 否则重启一次就丢持仓, 而且不报错。

⚠️ 恢复时 ``symbols`` / ``data_source`` / ``start_date`` 来自落盘时的
``_metadata``: 这几个参数决定取数窗口, 而游标存的是**数据行下标**,
窗口一变下标就指向别的日期, 恢复出来的"进度"是错的。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from stock_model.paper.engine import PaperEngine
    from stock_model.paper.models import Account

DISCLAIMER = "模拟盘 · 非真实交易, 不构成投资建议"

# 退化股票池: 动态选股不可用时使用。
# ⚠️ 这三只同属银行地产，属"伪分散"，会显著扭曲策略评估结论
# (上涨市踏空 / 下跌市超额虚高)，详见 docs/strategy-evaluation-2026-10-07.md
FALLBACK_SYMBOLS = ["000002", "000001", "600036"]

# 进程内单例(单 worker 场景足够, 与现有 pipeline 状态一致)
_engines: dict[str, PaperEngine] = {}

# 每个账户一把可重入锁: 定时任务在线程池里跑, 而 API 请求在事件循环里跑,
# 两者会同时碰同一个引擎。引擎不是线程安全的 —— 不加锁就可能出现
# 「手动点一下」和「定时跑一轮」同时撮合, 同一笔挂单被撮两次。
_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()
# 正在构造中的账户 → Event(构造完成时 set)。用于"并发首次请求只构造一次",
# 同时**不持账户锁**做慢速 IO(见 _get_engine 的警告注释)。
_building: dict[str, threading.Event] = {}


def _account_lock(account_id: str) -> threading.RLock:
    with _locks_guard:
        if account_id not in _locks:
            _locks[account_id] = threading.RLock()
        return _locks[account_id]


def _engine_metadata(engine: PaperEngine) -> dict[str, Any]:
    """引擎状态 → 落盘的 ``_metadata``(含游标, 丢了就会重放历史)"""
    state: dict[str, Any] = engine.state_dict()
    return state


def _persist(account_id: str, engine: PaperEngine) -> tuple[bool, str]:
    """落盘账户 + 引擎进度

    Returns:
        ``(是否成功, 失败原因)``。**失败不抛异常**, 而是把原因交回调用方,
        由调用方放进响应体 / 调度状态里 —— 定时场景下"跑成功但没存下来"
        等同于失败, 绝不能静默吞掉。

    实现上刻意不往 engine 上挂 ``_last_persist_error``: 那个属性不属于引擎,
    动态挂载会绕过类型检查(mypy 的 no-any-return 就出在这附近),
    也会让"错误传递"变成隐性契约。
    """
    from stock_model.paper.store import save_account

    try:
        save_account(engine.account, metadata=_engine_metadata(engine))
    except OSError as e:
        reason = f"{type(e).__name__}: {e}"
        logger.error(f"模拟盘状态落盘失败 account={account_id}: {reason}")
        return False, reason
    return True, ""


# 模拟盘取数的滚动窗口长度(bar 数)。
#
# 为什么用"最近 N 根"而不是绝对日期(2026-10-21 用户实测发现):
#   原先 _default_start_date 写死 "20240101", 账户从 2024-01-02 开始每
#   交易日爬一根。今天是 2026-10-21, cursor 才到 2024-04-17 —— **差 2.4 年**。
#   按这速度攒 20 笔成交要一两年, 且评估的是 2024 年的市场, 对当下没参考价值。
#
# 300 根的由来: 指标最长回看是 MA250(settings.analysis.ma_periods 含 250),
# 实测 250 根时末行 NaN 归零; 取 300 留 50 根余量, 让策略在窗口头部也有数据。
DEFAULT_LOOKBACK_BARS = 300


def migrate_to_rolling_window(account_file: Path) -> dict[str, Any]:
    """把已有账户迁移到滚动窗口口径(方案 A, 用户选定 A1 重置)

    为什么需要"重置"而不只是换 start_date:
      旧窗口(2024-01 起)产生的成交/持仓, 与新窗口(最近 300 根)的历史
      **不是同一口径** —— 保留它们会让"窗口第 0 根的起点持仓"与该窗口
      之前发生了什么对不上。观察期要的是同一口径下的连续记录,
      故按用户选定的 A1: 现金回初始、清持仓/成交/曲线, cursor 归 0。

    保留(用户配置, 不是历史): symbols / data_source / initial_capital。

    安全性:
      - 迁移前**必须备份**(原样复制到 ``*.bak_migrate_<ts>``)
      - 幂等: 已迁移过的账户再跑一次不报错、不二次破坏
      - 文件不存在显式 FileNotFoundError(不静默"成功")

    Returns:
        {"backup": 备份路径, "start_date": 新起点, "cursor": 新游标, ...}
    """
    import json
    import shutil
    from datetime import datetime

    path = Path(account_file)
    if not path.exists():
        raise FileNotFoundError(f"账户文件不存在: {path}")

    data = json.loads(path.read_text(encoding="utf-8"))
    meta = data.setdefault("_metadata", {})
    symbols = [str(s) for s in (meta.get("symbols") or []) if str(s).strip()]
    if not symbols:
        raise ValueError(f"账户 {path} 无股票池记录, 无法定窗口")

    # 1. 算窗口起点(与 _build_engine 同源, 保证两边一致)
    start_date = _rolling_start_date(symbols, str(meta.get("data_source") or "baostock"))

    # 2. 备份(原样, 迁移前)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = path.with_name(f"{path.stem}.bak_migrate_{ts}{path.suffix}")
    shutil.copy2(path, backup)

    # 3. 重置到窗口起点
    initial_capital = float(meta.get("initial_capital") or data.get("initial_capital") or 10000.0)
    meta["start_date"] = start_date
    meta["cursor"] = dict.fromkeys(symbols, 0)
    data["cash"] = initial_capital
    data["initial_capital"] = initial_capital
    data["positions"] = {}
    data["trades"] = []
    data["equity_curve"] = []

    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8", newline="")

    logger.info(
        f"[paper-migrate] 账户 {path} 已迁移到滚动窗口: "
        f"start_date={start_date} cursor=0 成交/持仓/曲线已重置 (备份 {backup.name})"
    )
    return {
        "backup": str(backup),
        "start_date": start_date,
        "cursor": 0,
        "symbols": symbols,
        "initial_capital": initial_capital,
    }


def _window_start_date(
    bars: Any, lookback: int = DEFAULT_LOOKBACK_BARS, default: str = "20200101"
) -> str:
    """由索引推出"最近 N 根"的起点日期(YYYYMMDD)

    Args:
        bars: DatetimeIndex 或日期序列(数据源返回的索引)
        lookback: 要覆盖多少根 bar
        default: 拿不到索引时的回退起点

    Returns:
        YYYYMMDD 字符串。**一定是 bars 里真实存在的交易日** ——
        否则取数区间与落盘不一致会导致 cursor(行下标)与日期错位。
    """
    if bars is None or len(bars) == 0:
        return default
    try:
        n = len(bars)
        if n <= lookback:
            return str(bars[0].date()).replace("-", "")
        return str(bars[n - lookback].date()).replace("-", "")
    except (AttributeError, IndexError, TypeError, ValueError):
        return default


def _rolling_start_date(
    symbols: list[str], data_source: str, lookback: int = DEFAULT_LOOKBACK_BARS
) -> str:
    """取"最近 N 根"对应的起点日期(YYYYMMDD)

    实现要点: 必须**先取一次数**才知道索引长度与真实交易日 ——
    不能按自然日倒推(那样算出的日期可能不是交易日, 取回来首根就偏移)。

    取数失败(网络抖动/源不可用)时回退到 ``_default_start_date``:
    起点只是取数区间, 引擎照常能跑; 而"因为算不出起点就开不了账户"
    是把优化变成了硬依赖, 不可接受。
    """
    from stock_model.data.fetcher import StockDataFetcher

    try:
        fetcher = StockDataFetcher(source=data_source)
        for sym in symbols:
            bars = fetcher.get_daily(sym, start_date=None)
            if bars is not None and not bars.empty:
                return _window_start_date(bars.index, lookback)
    except Exception as e:
        logger.warning(
            f"滚动窗口起点计算失败({type(e).__name__}: {e}), "
            f"回退默认起点 {_default_start_date(symbols)}"
        )
    return _default_start_date(symbols)


def _default_start_date(symbols: list[str]) -> str:
    """默认取数起点(已废弃: 改为滚动窗口, 保留仅为兼容既有调用方)"""
    return "20190101" if len(symbols) > 3 else "20240101"


def _build_engine(
    account_id: str, metadata: dict[str, Any], account: Account | None
) -> PaperEngine:
    """按落盘配置构造引擎

    恢复场景(``account`` 非空)传 ``validate_symbols=False``:
    池子此前已验证过, 再逐只取数一遍既慢, 又会让一次网络抖动
    变成"账户打不开"。账户通过构造参数注入, 使 ``Broker`` 正确绑定它。
    """
    from stock_model.paper.engine import PaperEngine
    from stock_model.strategy.manual import ManualStrategy

    symbols = [str(s) for s in (metadata.get("symbols") or []) if str(s).strip()]
    restoring = account is not None
    if not symbols:
        if restoring:  # pragma: no cover - 落盘时池与账户同时写, 不会只有一边
            logger.warning(f"账户 {account_id} 有状态但无股票池记录, 回退默认池")
        symbols = list(FALLBACK_SYMBOLS)

    data_source = str(metadata.get("data_source") or "baostock")
    # 取数起点:
    #   - 落盘显式配了 start_date  -> 按它(可回退旧行为, 也便于对齐历史账户)
    #   - 否则 -> **滚动窗口**(最近 DEFAULT_LOOKBACK_BARS 根)
    # 写死绝对日期会让账户永远爬历史: 2026-10-21 实测 cursor 才到 2024-04-17,
    # 比"今天"慢 2.4 年, 观察期失去意义(见 DEFAULT_LOOKBACK_BARS 注释)。
    start_date = metadata.get("start_date")
    if not start_date:
        start_date = _rolling_start_date(symbols, data_source)

    engine = PaperEngine(
        symbols=symbols,
        initial_capital=float(metadata.get("initial_capital") or 10000.0),
        data_source=data_source,
        # 恢复时取数区间必须与落盘时一致, 否则游标(行下标)与日期错位
        start_date=str(start_date),
        end_date=metadata.get("end_date"),
        account_id=account_id,
        account=account,
        validate_symbols=not restoring,
    )
    engine.add_strategy(ManualStrategy())

    if restoring:
        engine.load_state(metadata)
    return engine


def _get_engine(account_id: str = "default") -> PaperEngine:
    """取或建引擎实例(进程重启后从磁盘恢复)

    ⚠️ **慢速构造(数据源 login/取数)必须在锁外**:
    早期实现把 ``_build_engine`` 放在 ``_account_lock`` 临界区内 ——
    baostock 挂起时 login 能卡 20s+, 而锁是**每账户**的, 于是该账户的
    所有请求(连只读的 schedule 状态查询)全部排队, 前端表现是
    「点了开启定时没反应」(2026-10-13 E2E 实测)。
    现在锁只保护 ``_engines`` 字典的读写, 构造本身不持锁;
    「只构造一次」由字典双检 + 构造占位保证。
    """
    if account_id in _engines:
        return _engines[account_id]

    # 锁外构造(慢): 用占位对象防并发重复构造
    with _locks_guard:
        if account_id in _building:
            placeholder = _building[account_id]
        else:
            placeholder = threading.Event()
            _building[account_id] = placeholder
            placeholder = None  # 我负责构造

    if placeholder is not None:
        # 别人正在构造: 等它完成(不持账户锁, 不阻塞别的请求)
        placeholder.wait(timeout=300)
        if account_id in _engines:
            return _engines[account_id]
        # 构造失败/超时: 自己再试一次(锁外)
        return _construct_engine(account_id)

    try:
        return _construct_engine(account_id)
    finally:
        with _locks_guard:
            ev = _building.pop(account_id, None)
        if ev is not None:
            ev.set()


def _construct_engine(account_id: str) -> PaperEngine:
    """真正构造引擎(慢, 锁外) + 双检落库"""
    if account_id in _engines:
        return _engines[account_id]
    from stock_model.paper.store import load_account, load_metadata

    account = load_account(account_id)
    metadata = load_metadata(account_id)

    if account is not None:
        logger.info(
            f"账户 {account_id} 从磁盘恢复: 持仓 {len(account.positions)}, "
            f"成交 {len(account.trades)}, 游标 {len(metadata.get('cursor') or {})} 只"
        )
    else:
        logger.info(f"账户 {account_id} 无落盘状态, 新建")

    engine = _build_engine(account_id, metadata, account)
    _engines[account_id] = engine
    return engine


def _rebuild_engine(account_id: str, symbols: list[str]) -> PaperEngine:
    """按新股票池重建引擎(**开一轮新模拟**)

    为什么换池要连账户一起重置, 而不是只换标的列表:
    账户里的成交与资金曲线是**在旧池子上**产生的。只换标的、留着旧曲线,
    等于把两段不同标的的历史拼成一条 —— 之后算出来的夏普、回撤、超额
    都是无意义的混合体(本项目最怕的"看着正常的错结果")。
    故与原实现保持一致: 换池 = 重新开始, 但**显式告知**(日志 + 响应里的
    ``account_reset``), 并立刻落盘, 免得重启后旧账户又"复活"。
    """
    from stock_model.paper.engine import PaperEngine
    from stock_model.paper.store import load_metadata
    from stock_model.strategy.manual import ManualStrategy

    if not symbols:
        return _get_engine(account_id)

    base = _get_engine(account_id)  # 取现用数据源/区间作为默认
    meta = load_metadata(account_id)

    engine = PaperEngine(
        symbols=symbols,
        initial_capital=float(meta.get("initial_capital") or 10000.0),
        data_source=str(meta.get("data_source") or base.data_source),
        start_date=str(meta.get("start_date") or _default_start_date(symbols)),
        end_date=meta.get("end_date"),
        account_id=account_id,
    )
    engine.add_strategy(ManualStrategy())
    _engines[account_id] = engine
    logger.warning(
        f"股票池已更换, 账户 {account_id} 重置并开始新一轮模拟"
        f"(旧成交/资金曲线不再混入): {engine.symbols}"
    )
    return engine


def run_paper_cycle(account_id: str, days: int) -> dict[str, Any]:
    """推进 N 个交易日并落盘(定时任务与手动执行共用的执行体)

    这是 ``PaperScheduler`` 注入的执行体 —— 定时跑与手动跑必须走同一条路,
    否则"手动能跑通"证明不了"定时能跑通"。
    """
    with _account_lock(account_id):
        engine = _get_engine(account_id)
        steps = engine.run(days=max(1, int(days)))
        persisted, persist_error = _persist(account_id, engine)

        # 池子被剔空 → **不当作成功**。
        # 观察期实测(2026-10-12 首个交易日): 3 只标的取数全部失败被剔除,
        # step() 返回 no_symbols, 而这里仍返回 ok —— 调度记录 ✅、无告警,
        # 用户以为"每天在跑", 其实之后每天都空跑(缓存+落盘互为锁, 永远救不回来)。
        # 这正是本项目最怕的"看着正常的假成功", 必须显式暴露。
        if not steps and not engine.symbols:
            dropped = ", ".join(engine.dropped_symbols[:5]) or "(无记录)"
            return {
                "status": "pool_empty",
                "steps": 0,
                "from": "",
                "to": "",
                "trades": len(engine.account.trades),
                "total_asset": round(engine.account.total_asset, 2),
                "symbols": [],
                "pool_empty_reason": (
                    f"股票池已空: 全部 {len(engine.dropped_symbols)} 只标的取数失败"
                    f"被剔除({dropped})。请检查数据源或换池(POST /api/paper/universe)"
                ),
                "persisted": persisted,
                "persist_error": persist_error,
                "disclaimer": DISCLAIMER,
            }

        return {
            "status": "ok",
            "steps": len(steps),
            "from": steps[0]["date"] if steps else "",
            "to": steps[-1]["date"] if steps else "",
            "trades": len(engine.account.trades),
            "total_asset": round(engine.account.total_asset, 2),
            "symbols": engine.symbols,
            "persisted": persisted,
            "persist_error": persist_error,
            "disclaimer": DISCLAIMER,
        }


def configure_paper_scheduler() -> Any:
    """装配模拟盘调度器：执行体 + **告警通道**

    app 启动恢复与 API 首次使用共用这一个入口。刻意只有一个入口 ——
    两处各配一半(比如启动时漏了告警)会让「失败主动提醒」在某些路径上静默失效，
    而这类"某条路径上没生效"正是本项目反复踩的坑。

    告警通道来自 ``STOCK_NOTIFY_*`` 配置(见 ``.env.example``)：默认只有控制台，
    配了 webhook 就能推到钉钉/飞书/企业微信。
    """
    from stock_model.config.settings import get_settings
    from stock_model.notify import build_alert_notifier
    from stock_model.paper.scheduler import get_scheduler

    sched = get_scheduler()
    sched.set_runner(run_paper_cycle)

    cfg = get_settings().notify
    notifier = build_alert_notifier(cfg)
    sched.set_alerter(
        notifier.notify_message,
        description=notifier.description,
        every_n_failures=cfg.alert_every_n_failures,
    )
    return sched


def _scheduler() -> Any:
    """取调度器单例(已装配好执行体与告警通道)"""
    return configure_paper_scheduler()


def register_paper_routes(app: Any) -> None:
    """挂载模拟盘相关路由"""

    @app.get("/api/paper/account")
    async def paper_account(account_id: str = "default"):
        """账户总览"""
        engine = _get_engine(account_id)
        acc = engine.account
        return {
            "initial_capital": acc.initial_capital,
            "cash": round(acc.cash, 2),
            "market_value": round(acc.market_value, 2),
            "total_asset": round(acc.total_asset, 2),
            "total_return": round(acc.total_return, 4),
            "position_ratio": round(acc.position_ratio, 4),
            "trading_days": len(acc.equity_curve),
            "disclaimer": DISCLAIMER,
        }

    @app.get("/api/paper/positions")
    async def paper_positions(account_id: str = "default"):
        """当前持仓"""
        engine = _get_engine(account_id)
        items = [p.to_dict() for p in engine.account.positions.values()]
        return {
            "positions": items,
            "count": len(items),
            "disclaimer": DISCLAIMER,
        }

    @app.get("/api/paper/trades")
    async def paper_trades(account_id: str = "default", limit: int = 100):
        """成交记录(倒序)"""
        engine = _get_engine(account_id)
        items = [t.to_dict() for t in engine.account.trades][-limit:][::-1]
        return {
            "trades": items,
            "total": len(engine.account.trades),
            "disclaimer": DISCLAIMER,
        }

    @app.get("/api/paper/metrics")
    async def paper_metrics(account_id: str = "default", benchmark: str = "000002"):
        """绩效指标(含可靠性标记)"""
        engine = _get_engine(account_id)
        report = engine.report(benchmark_symbol=benchmark)
        report["disclaimer"] = DISCLAIMER
        return report

    @app.get("/api/paper/equity")
    async def paper_equity(account_id: str = "default"):
        """资金曲线"""
        engine = _get_engine(account_id)
        items = [p.to_dict() for p in engine.account.equity_curve]
        return {"equity": items, "count": len(items), "disclaimer": DISCLAIMER}

    @app.post("/api/paper/step")
    async def paper_step(payload: dict | None = None):
        """推进一个交易日"""
        account_id = (payload or {}).get("account_id", "default")
        with _account_lock(account_id):
            engine = _get_engine(account_id)
            try:
                result = engine.step()
            except Exception as e:
                logger.error(f"模拟盘推进失败: {e}")
                return {"status": "error", "error": f"{type(e).__name__}: {e}"}

            persisted, persist_error = _persist(account_id, engine)
            result["persisted"] = persisted
            if not persisted:
                result["persist_error"] = persist_error
            return result

    @app.post("/api/paper/run")
    async def paper_run(payload: dict | None = None):
        """连续推进 N 个交易日

        payload 可传 ``symbols`` 用动态股票池替换默认列表。
        """
        payload = payload or {}
        account_id = payload.get("account_id", "default")
        symbols = payload.get("symbols")
        days = int(payload.get("days", 60))
        days = max(1, min(days, 1500))

        with _account_lock(account_id):
            engine = _get_engine(account_id)
            pool_changed = False
            try:
                if symbols:
                    # 用调用方指定的股票池重建引擎(动态选股结果)
                    engine = _rebuild_engine(
                        account_id,
                        [s.strip() for s in symbols if s.strip()],
                    )
                    pool_changed = True
                steps = engine.run(days=days)
            except Exception as e:
                logger.error(f"模拟盘批量执行失败: {e}")
                return {"status": "error", "error": f"{type(e).__name__}: {e}"}

            persisted, persist_error = _persist(account_id, engine)
            return {
                "status": "ok",
                "steps": len(steps),
                "from": steps[0]["date"] if steps else "",
                "to": steps[-1]["date"] if steps else "",
                "trades": len(engine.account.trades),
                "total_asset": round(engine.account.total_asset, 2),
                "symbols": engine.symbols,
                "account_reset": pool_changed,
                "persisted": persisted,
                "persist_error": persist_error,
                "disclaimer": DISCLAIMER,
            }

    @app.get("/api/paper/universe")
    async def paper_universe(top_n: int = 20, min_amount: float = 5e7):
        """动态构建候选股票池(替代人工指定的股票列表)

        ⚠️ 耗时较长: 需逐只查询估值/流动性数据, 20-40 只约 1-2 分钟。
        """
        try:
            from stock_model.paper.universe import UniverseConfig, UniverseSelector

            cfg = UniverseConfig(
                top_n=max(5, min(top_n, 50)),
                min_amount=min_amount,
                pre_screen_top=max(40, top_n * 3),
            )
            candidates = UniverseSelector(cfg).build()
            return {
                "candidates": [c.to_dict() for c in candidates],
                "count": len(candidates),
                "config": cfg.describe(),
                "disclaimer": DISCLAIMER,
            }
        except Exception as e:
            logger.error(f"股票池构建失败: {e}")
            return {"error": f"{type(e).__name__}: {str(e)[:200]}", "candidates": []}

    @app.post("/api/paper/reset")
    async def paper_reset(payload: dict | None = None):
        """重置账户(同时清掉落盘状态, 否则重启会"复活"旧账户)"""
        from stock_model.paper.store import delete_account

        account_id = (payload or {}).get("account_id", "default")
        with _account_lock(account_id):
            _engines.pop(account_id, None)
            deleted = delete_account(account_id)
            engine = _get_engine(account_id)
            _persist(account_id, engine)
            return {
                "status": "reset",
                "account_id": account_id,
                "state_file_removed": deleted,
                "disclaimer": DISCLAIMER,
            }

    # ---- 定时运行 ----

    @app.post("/api/paper/schedule")
    async def paper_schedule_start(payload: dict | None = None):
        """开启定时运行

        payload::

            {"account_id": "default", "hour": 15, "minute": 30, "days": 1}
            {"account_id": "default", "interval_minutes": 60, "days": 1}

        默认每天 15:30(A 股收盘后 30 分钟)推进 1 个交易日;
        周末由 cron 结构性排除, 节假日按 ``data/paper/holidays.json`` 排除。

        缺 apscheduler 时返回 **503**(而不是 200 + 错误字段)——
        与 ``/api/pipeline/start`` 的处理一致: 服务本身正常, 只是缺一个
        可选依赖, 且必须让调用方知道"没跑起来"。
        """
        from fastapi import HTTPException

        from stock_model.data.collector import MissingDependencyError

        payload = payload or {}
        account_id = payload.get("account_id", "default")
        try:
            return _scheduler().start(
                account_id,
                hour=payload.get("hour"),
                minute=payload.get("minute"),
                days=int(payload.get("days", 1)),
                interval_minutes=payload.get("interval_minutes"),
                timezone=str(payload.get("timezone") or "Asia/Shanghai"),
            )
        except MissingDependencyError as e:
            logger.error(f"模拟盘定时运行不可用: {e}")
            raise HTTPException(status_code=503, detail=str(e)) from e
        except (ValueError, TypeError) as e:
            logger.error(f"模拟盘定时参数不合法: {e}")
            raise HTTPException(status_code=400, detail=f"{type(e).__name__}: {e}") from e

    @app.delete("/api/paper/schedule")
    async def paper_schedule_stop(account_id: str = "default"):
        """停止定时运行"""
        return _scheduler().stop(account_id)

    @app.get("/api/paper/schedule")
    async def paper_schedule_status(account_id: str | None = None):
        """查询定时运行状态(含最近一次执行的成败原因)"""
        return _scheduler().status(account_id)

    @app.post("/api/paper/schedule/run")
    async def paper_schedule_run_now(payload: dict | None = None):
        """立即执行一次定时任务(与定时触发走同一条路径)

        用于验证「定时到底能不能跑通」, 不必等到 15:30。
        """
        account_id = (payload or {}).get("account_id", "default")
        return _scheduler().run_now(account_id)
