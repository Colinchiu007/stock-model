"""模拟盘引擎

把「拉数据 → 跑策略 → 生成订单 → T+1 撮合 → 估值」串成一条可推进的流水线。

与回测的关键差异
----------------
回测一次性遍历历史；模拟盘按 **真实时间线** 逐日推进，且**只用截止到当日的数据**。

时间线示例::

    D日: 拉数据到 D 收盘 → 策略看 df[:D] → BUY 信号 → 挂单(不成交)
    D+1日: 拉 D+1 开盘价 → 撮合挂单 → 成交 → 更新持仓 → 收盘估值

这样保证信号产生时不含有未来信息。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger

from stock_model.paper.broker import Broker
from stock_model.paper.models import Account, Order, Side

if TYPE_CHECKING:
    import pandas as pd

    from stock_model.strategy.base import BaseStrategy


def _data_error_types() -> tuple[type[Exception], ...]:
    """数据获取可能抛出的异常类型

    注意 ``tenacity.RetryError`` 直接继承 ``Exception`` 而**非**
    ``RuntimeError`` —— 只捕 ``RuntimeError`` 会漏掉它, 导致
    「取数失败」以未处理异常穿透(实测踩过)。
    """
    types: list[type[Exception]] = [
        ValueError,
        KeyError,
        IndexError,
        RuntimeError,  # fetcher 在取数失败时抛的正是这个
    ]
    try:
        from tenacity import RetryError

        types.append(RetryError)
    except ImportError:  # pragma: no cover
        pass
    return tuple(types)


_DATA_ERRORS = _data_error_types()


class PaperEngine:
    """模拟盘引擎

    使用示例::

        engine = PaperEngine(symbols=["000002"], initial_capital=10000)
        engine.add_strategy(ManualStrategy())
        engine.step()      # 推进一个交易日
        report = engine.report()
    """

    def __init__(
        self,
        symbols: list[str],
        initial_capital: float = 10000.0,
        data_source: str = "baostock",
        start_date: str = "20240101",
        end_date: str | None = None,
        max_position_pct: float = 0.20,
        min_data_rows: int = 60,
        skip_unavailable: bool = True,
    ) -> None:
        """构造模拟盘引擎

        Args:
            skip_unavailable: 池中有股票取不到数据时, 是剔除它们(True)
                还是直接抛错(False)。**无论该开关如何, 至少要有一只
                股票可用** —— 全部取不到时一律抛错, 因为"静默跑一个
                空池"比崩溃更难发现(这是本项目反复出现的缺陷模式)。
        """
        self.data_source = data_source
        self.start_date = start_date
        self.end_date = end_date
        self.min_data_rows = min_data_rows

        self.dropped_symbols: list[str] = []
        if len(symbols) > 1:
            if skip_unavailable:
                symbols = self._filter_tradable(symbols)
            else:
                self._assert_all_tradable(symbols)
        elif symbols:
            self._assert_all_tradable(symbols)
        self.symbols = symbols

        self.account = Account(
            account_id="paper-default",
            initial_capital=initial_capital,
            cash=initial_capital,
        )
        self.broker = Broker(self.account, max_position_pct=max_position_pct)
        self._strategies: list[BaseStrategy] = []
        self._cursor: dict[str, int] = {}  # 各标的已处理到的数据行索引

    # ==================== 策略 ====================

    def add_strategy(self, strategy: BaseStrategy) -> None:
        self._strategies.append(strategy)
        logger.info(f"注册策略: {strategy.name}")

    # ==================== 数据 ====================

    def _filter_tradable(self, symbols: list[str]) -> list[str]:
        """剔除在回测区间取不到数据的股票

        次新股/停牌股在某些区间无行情。若不剔除, ``step()`` 会在
        首次取数时抛 RuntimeError, 整个回测中断。
        """
        from stock_model.data.fetcher import StockDataFetcher

        fetcher = StockDataFetcher(source=self.data_source)
        ok: list[str] = []
        dropped: list[str] = []
        for sym in symbols:
            try:
                df = fetcher.get_daily(sym, start_date=self.start_date, end_date=self.end_date)
                if df is not None and not df.empty and len(df) >= self.min_data_rows:
                    ok.append(sym)
                else:
                    dropped.append(sym)
            except _DATA_ERRORS as e:
                dropped.append(sym)
                logger.warning(f"剔除 {sym}: {type(e).__name__}")

        if dropped:
            self.dropped_symbols = dropped
            logger.warning(f"区间 {self.start_date}~{self.end_date} 剔除不可用标的: {dropped}")
        if not ok:
            raise RuntimeError(f"区间 {self.start_date}~{self.end_date} 内 {symbols} 全部无数据")
        return ok

    def _assert_all_tradable(self, symbols: list[str]) -> None:
        """严格模式: 任一股票取不到数据即抛错

        至少要有一只可用 —— 全部不可用时静默继续会产出"空回测"，
        看起来成功实则毫无意义。
        """
        from stock_model.data.fetcher import StockDataFetcher

        fetcher = StockDataFetcher(source=self.data_source)
        usable = 0
        for sym in symbols:
            try:
                df = fetcher.get_daily(sym, start_date=self.start_date, end_date=self.end_date)
                if df is not None and not df.empty and len(df) >= self.min_data_rows:
                    usable += 1
            except _DATA_ERRORS:
                continue
        if usable == 0:
            raise RuntimeError(f"区间 {self.start_date}~{self.end_date} 内 {symbols} 全部无数据")

    def _load(self, symbol: str) -> pd.DataFrame:
        """加载行情(带缓存)"""
        from stock_model.data.fetcher import StockDataFetcher

        if not hasattr(self, "_fetcher"):
            self._fetcher = StockDataFetcher(source=self.data_source)
        return self._fetcher.get_daily(symbol, start_date=self.start_date, end_date=self.end_date)

    def _bars(self, symbol: str) -> pd.DataFrame:
        """取行情(带缓存)"""
        if not hasattr(self, "_data_cache"):
            self._data_cache: dict[str, pd.DataFrame] = {}
        if symbol not in self._data_cache:
            self._data_cache[symbol] = self._load(symbol)
        return self._data_cache[symbol]

    # ==================== 推进 ====================

    def step(self) -> dict[str, Any]:
        """推进一个交易日

        流程:
          1. 撮合昨日挂单(用今日开盘价)
          2. 今日收盘后跑策略生成新订单
          3. 按今日收盘价估值并记录资金曲线

        Returns:
            本步摘要
        """
        # 1. 定位今日(各标的按索引推进, 取最短的保证都是 T+1)
        indices = []
        frames = {}
        order: list[str] = []
        for symbol in list(self.symbols):
            # 单只取数失败不应拖垮整步: 剔除后继续用其余标的
            try:
                df = self._bars(symbol)
            except _DATA_ERRORS as e:
                logger.warning(f"step: 剔除取数失败标的 {symbol} ({type(e).__name__})")
                self.symbols = [s for s in self.symbols if s != symbol]
                if symbol not in self.dropped_symbols:
                    self.dropped_symbols.append(symbol)
                continue

            if len(df) < self.min_data_rows:
                logger.warning(f"step: 剔除数据不足标的 {symbol} ({len(df)}行)")
                self.symbols = [s for s in self.symbols if s != symbol]
                if symbol not in self.dropped_symbols:
                    self.dropped_symbols.append(symbol)
                continue

            frames[symbol] = df
            order.append(symbol)
            idx = self._cursor.get(symbol, 0) + 1  # T+1: 从昨日位置 +1
            if idx >= len(df):
                logger.info(f"{symbol} 数据已到最新({df.index[-1].date()}), 无 T+1 可推进")
                return {"status": "no_more_data", "date": "", "symbol": symbol}
            indices.append(idx)
            self._cursor[symbol] = idx

        if not indices or not order:
            return {"status": "no_symbols", "date": "", "fills": []}

        i = min(indices)  # 用最小索引, 保证不会取到未来日期
        date = str(frames[order[0]].index[i].date())
        today = {s: frames[s].iloc[i] for s in order}

        # 2. 撮合昨日挂单(T+1 开盘价)
        new_day = self.account.last_trade_date != date
        if new_day:
            self.broker.unfreeze_all()  # 隔夜解冻
        fills = (
            self.broker.match_by_date(today, date)
            if hasattr(self.broker, "match_by_date")
            else self._match_each(today, date)
        )

        # 3. 今日收盘后跑策略 -> 挂单
        signals = self._run_strategies(frames, i)
        orders = self._make_orders(signals, date)

        # 4. 收盘估值
        prices = {s: float(b["close"]) for s, b in today.items()}
        total = self.broker.mark_to_market(prices, date)

        return {
            "status": "ok",
            "date": date,
            "fills": [t.to_dict() for t in fills],
            "signals": signals,
            "orders": [o.to_dict() for o in orders],
            "total_asset": round(total, 2),
            "cash": round(self.account.cash, 2),
            "position_ratio": round(self.account.position_ratio, 4),
        }

    def _match_each(self, today: dict, date: str) -> list:
        """按标的分别撮合(每个标的只撮合自己的订单)"""
        fills = []
        by_symbol: dict[str, list[Order]] = {}
        for o in self.account.pending_orders:
            by_symbol.setdefault(o.symbol, []).append(o)

        for symbol, orders in by_symbol.items():
            bar = today.get(symbol)
            if bar is None:
                continue
            # 只取该标的的订单
            self.account.pending_orders = orders
            fills.extend(self.broker.match(bar, date))

        # 恢复未处理的其它标的订单
        self.account.pending_orders = [
            o for o in self.account.pending_orders if o.status.value == "pending"
        ]
        return fills

    def _run_strategies(self, frames: dict, i: int) -> list[dict[str, Any]]:
        """用截止到第 i 天(含)的数据跑策略"""
        signals = []
        for symbol, df in frames.items():
            if i < self.min_data_rows:
                continue
            current_df = df.iloc[: i + 1]
            for strategy in self._strategies:
                try:
                    result = strategy.analyze(symbol, current_df)
                except (ValueError, KeyError, TypeError) as e:
                    logger.warning(f"{symbol} 策略 {strategy.name} 执行失败: {e}")
                    continue
                signals.append(
                    {
                        "symbol": symbol,
                        "strategy": strategy.name,
                        "action": result.action.value,
                        "confidence": round(result.confidence, 3),
                        "reason": result.reason,
                        "target_price": result.target_price,
                        "stop_loss": result.stop_loss,
                        "position_pct": result.position_pct,
                        "price": round(float(current_df["close"].iloc[-1]), 4),
                    }
                )
        return signals

    def _make_orders(self, signals: list[dict[str, Any]], date: str) -> list[Order]:
        """由信号生成订单

        规则:
          - HOLD 不下单
          - 已有持仓时 SELL 才下单(不重复买)
          - 无持仓时 BUY 下单
        """
        orders: list[Order] = []
        for sig in signals:
            symbol = sig["symbol"]
            pos = self.account.get_position(symbol)
            action = sig["action"]

            if action == "hold":
                continue
            if action == "buy" and pos is None:
                side = Side.BUY
            elif action == "sell" and pos is not None and pos.available_shares > 0:
                side = Side.SELL
            else:
                continue

            if side == Side.BUY:
                # 按建议仓位比例计算股数(价格用现价, 由调用方注入到 signals)
                pct = max(0.01, min(0.20, sig.get("position_pct", 10) / 100))
                ref_price = sig.get("price") or 10.0
                if ref_price <= 0:
                    continue
                shares = int(self.account.cash * pct / ref_price / 100) * 100
                if shares < 100:
                    continue
            else:
                shares = pos.available_shares  # type: ignore[union-attr]

            order = Order(
                symbol=symbol,
                side=side,
                shares=shares,
                signal_action=action.upper(),
                signal_confidence=sig["confidence"],
                signal_reason=sig["reason"],
                created_date=date,
            )
            self.broker.submit_order(order)
            orders.append(order)
        return orders

    # ==================== 报告 ====================

    def report(self, benchmark_symbol: str | None = None) -> dict[str, Any]:
        """生成绩效报告(含可靠性提示)

        基准的取数**独立于回测池**: 基准通常不在 symbols 里(动态池尤其如此),
        不能依赖 ``_cursor`` 判断推进到哪一天 —— 否则基准为空、超额恒为 0,
        会得出"改善 39 个百分点"这类完全虚假的结论(实测踩过)。
        """
        from stock_model.paper.metrics import evaluate

        benchmark_prices = None
        benchmark_return = None
        if benchmark_symbol:
            try:
                from stock_model.data.fetcher import StockDataFetcher

                fetcher = StockDataFetcher(source=self.data_source)
                bdf = fetcher.get_daily(
                    benchmark_symbol, start_date=self.start_date, end_date=self.end_date
                )
                if bdf is not None and not bdf.empty and len(bdf) >= 2:
                    benchmark_prices = [float(x) for x in bdf["close"]]
                    benchmark_return = benchmark_prices[-1] / benchmark_prices[0] - 1
            except _DATA_ERRORS as e:
                logger.warning(f"基准 {benchmark_symbol} 数据获取失败: {e}")

        if benchmark_symbol and benchmark_prices is None:
            logger.warning(
                f"基准 {benchmark_symbol} 无数据, 超额收益不可计算 —— 本次报告的 alpha 不可信"
            )

        m = evaluate(self.account, benchmark_prices)
        if benchmark_return is not None:
            # 以独立取数的结果为准, 覆盖 metrics 内部可能为 0 的值
            m.benchmark_return = benchmark_return
            m.alpha = m.total_return - benchmark_return

        return {
            "account": {
                "initial_capital": self.account.initial_capital,
                "cash": round(self.account.cash, 2),
                "market_value": round(self.account.market_value, 2),
                "total_asset": round(self.account.total_asset, 2),
                "total_return": round(self.account.total_return, 4),
                "position_ratio": round(self.account.position_ratio, 4),
            },
            "metrics": m.to_dict(),
            "benchmark_available": benchmark_prices is not None,
            "disclaimer": "模拟盘 · 非真实交易, 不构成投资建议",
        }

    def run(self, days: int | None = None) -> list[dict[str, Any]]:
        """连续推进 N 个交易日(None = 直到数据用尽)

        单只股票取数失败不应中断整轮回测 —— 构造时的可用性检查与
        ``_bars`` 的实际取数路径可能不完全一致(缓存/时间窗差异),
        故这里再兜一层: 剔除失败标的, 全部失败才抛错。
        """
        steps: list[dict[str, Any]] = []
        last_error: Exception | None = None

        for _ in range(days) if days else range(10_000):
            try:
                r = self.step()
            except _DATA_ERRORS as e:
                # 某只标的取数失败: 剔除后重试, 而不是让整轮崩掉
                last_error = e
                if len(self.symbols) <= 1:
                    raise
                bad = self._locate_failing_symbol()
                if bad is None:
                    raise
                logger.warning(f"剔除取数失败的标的: {bad} ({type(e).__name__})")
                self.symbols = [s for s in self.symbols if s != bad]
                self.dropped_symbols.append(bad)
                self._data_cache.pop(bad, None)
                continue

            if r["status"] != "ok":
                break
            steps.append(r)

        if not steps and last_error is not None:
            raise RuntimeError(f"回测未能推进任何交易日: {last_error}") from last_error
        return steps

    def _locate_failing_symbol(self) -> str | None:
        """定位哪只股票取数失败(逐只重试)"""
        for sym in list(self._data_cache.keys()):
            try:
                self._fetch_metrics_for(sym)
            except _DATA_ERRORS:
                return sym
        return None

    def _fetch_metrics_for(self, symbol: str) -> Any:
        """触发一次取数(用于探测可用性)"""
        return self._load(symbol)
