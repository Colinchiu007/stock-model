"""
策略引擎与回测引擎

StrategyEngine: 管理多策略执行、信号聚合、绩效追踪
BacktestEngine: 历史数据模拟回测、绩效指标计算

增强指标 (v0.5.0):
  - Calmar比率、最大回撤持续期、连续胜负、盈利因子、平均持仓
  - TradeAnalysis: 交易对分析(收益/持仓天数/最佳/最差)
  - 基准对比: 买入持有基准、Alpha、Beta、信息比率
  - 策略比较: compare_strategies 多策略对比报告
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from loguru import logger

from stock_model.strategy.base import ActionType, BaseStrategy, StrategyResult


@dataclass
class Trade:
    """交易记录"""

    symbol: str
    action: ActionType
    price: float
    shares: int
    timestamp: pd.Timestamp
    commission: float = 0.0

    @property
    def amount(self) -> float:
        """交易金额"""
        return self.price * self.shares + self.commission

    def __str__(self) -> str:
        return (
            f"Trade({self.symbol}: {self.action.value} @ {self.price:.2f} "
            f"x {self.shares}股, 佣金={self.commission:.2f})"
        )


@dataclass
class TradePair:
    """配对交易(买入→卖出)"""

    buy_trade: Trade
    sell_trade: Trade

    @property
    def return_pct(self) -> float:
        """收益率"""
        if self.buy_trade.price == 0:
            return 0.0
        return (self.sell_trade.price - self.buy_trade.price) / self.buy_trade.price

    @property
    def profit(self) -> float:
        """盈利金额"""
        buy_cost = self.buy_trade.price * self.buy_trade.shares + self.buy_trade.commission
        sell_income = self.sell_trade.price * self.sell_trade.shares - self.sell_trade.commission
        return sell_income - buy_cost

    @property
    def is_win(self) -> bool:
        """是否盈利"""
        return self.return_pct > 0

    @property
    def holding_days(self) -> int:
        """持仓天数"""
        if isinstance(self.buy_trade.timestamp, pd.Timestamp) and isinstance(
            self.sell_trade.timestamp, pd.Timestamp
        ):
            delta = self.sell_trade.timestamp - self.buy_trade.timestamp
            return max(1, delta.days)
        return 1

    def __str__(self) -> str:
        return (
            f"TradePair(收益={self.return_pct:.2%}, "
            f"持仓={self.holding_days}天, "
            f"盈利={self.profit:.2f})"
        )


@dataclass
class TradeAnalysis:
    """交易分析报告"""

    total_pairs: int = 0
    win_count: int = 0
    loss_count: int = 0
    win_rate: float = 0.0
    avg_return: float = 0.0
    avg_win_return: float = 0.0
    avg_loss_return: float = 0.0
    best_trade_return: float = 0.0
    worst_trade_return: float = 0.0
    avg_holding_days: float = 0.0
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0
    profit_factor: float = 0.0
    total_profit: float = 0.0
    total_loss: float = 0.0

    def __str__(self) -> str:
        return (
            f"TradeAnalysis(交易对={self.total_pairs}, "
            f"胜率={self.win_rate:.2%}, "
            f"盈利因子={self.profit_factor:.2f}, "
            f"平均持仓={self.avg_holding_days:.1f}天)"
        )


@dataclass
class BenchmarkResult:
    """基准对比结果"""

    strategy_return: float = 0.0
    benchmark_return: float = 0.0
    alpha: float = 0.0
    beta: float = 0.0
    information_ratio: float = 0.0
    tracking_error: float = 0.0

    def __str__(self) -> str:
        return (
            f"Benchmark(策略={self.strategy_return:.2%}, "
            f"基准={self.benchmark_return:.2%}, "
            f"Alpha={self.alpha:.2%}, "
            f"Beta={self.beta:.2f})"
        )


@dataclass
class BacktestResult:
    """回测结果"""

    strategy_name: str
    symbol: str
    initial_cash: float
    final_cash: float
    trades: list[Trade] = field(default_factory=list)
    equity_curve: pd.Series = field(default_factory=pd.Series)
    metrics: dict[str, float] = field(default_factory=dict)
    trade_analysis: TradeAnalysis = field(default_factory=TradeAnalysis)
    benchmark: BenchmarkResult = field(default_factory=BenchmarkResult)

    @property
    def total_return(self) -> float:
        """总收益率"""
        if self.initial_cash == 0:
            return 0.0
        return (self.final_cash - self.initial_cash) / self.initial_cash

    def __str__(self) -> str:
        return (
            f"BacktestResult({self.strategy_name}/{self.symbol}: "
            f"收益率={self.total_return:.2%}, 交易={len(self.trades)}笔)"
        )


class BacktestEngine:
    """回测引擎

    基于历史数据模拟策略执行，计算绩效指标。

    使用示例:
        engine = BacktestEngine(initial_cash=100000)
        result = engine.run(strategy, df, "000001")
        print(result.metrics)
    """

    def __init__(
        self,
        initial_cash: float = 100000.0,
        commission_rate: float = 0.0003,
        slippage: float = 0.001,
    ):
        self.initial_cash = initial_cash
        self.commission_rate = commission_rate  # 佣金率(万三)
        self.slippage = slippage  # 滑点(千一)

    def run(
        self,
        strategy: BaseStrategy,
        df: pd.DataFrame,
        symbol: str = "test",
    ) -> BacktestResult:
        """运行回测

        Args:
            strategy: 策略实例
            df: 厌史行情数据(需含close列)
            symbol: 股票代码

        Returns:
            回测结果
        """
        logger.info(f"回测开始: {strategy.name} / {symbol}, 初始资金={self.initial_cash}")

        cash = self.initial_cash
        position = 0  # 持仓股数
        trades: list[Trade] = []
        equity_history: list[float] = []

        if len(df) < 2:
            return BacktestResult(
                strategy_name=strategy.name,
                symbol=symbol,
                initial_cash=self.initial_cash,
                final_cash=cash,
                trades=trades,
                metrics={"total_return": 0.0},
            )

        # 滚动窗口回测
        window = min(60, len(df) - 1)
        df.index if df.index.name else range(len(df))

        for i in range(window, len(df)):
            # 截止到当前的数据
            current_df = df.iloc[: i + 1]
            current_price = float(df["close"].iloc[i])
            current_date = df.index[i] if isinstance(df.index, pd.DatetimeIndex) else i

            # 执行策略
            try:
                result = strategy.analyze(symbol, current_df)
            except Exception as e:
                logger.warning(f"回测第{i}步策略执行失败: {e}")
                equity_history.append(cash + position * current_price)
                continue

            # 交易逻辑
            if result.action == ActionType.BUY and position == 0:
                # 买入
                buy_price = current_price * (1 + self.slippage)
                max_shares = int(cash / (buy_price * 100)) * 100  # 100股整数倍
                if max_shares > 0:
                    commission = buy_price * max_shares * self.commission_rate
                    cash -= buy_price * max_shares + commission
                    position = max_shares
                    trades.append(
                        Trade(
                            symbol=symbol,
                            action=ActionType.BUY,
                            price=buy_price,
                            shares=max_shares,
                            timestamp=current_date,
                            commission=commission,
                        )
                    )

            elif result.action == ActionType.SELL and position > 0:
                # 卖出
                sell_price = current_price * (1 - self.slippage)
                commission = sell_price * position * self.commission_rate
                cash += sell_price * position - commission
                trades.append(
                    Trade(
                        symbol=symbol,
                        action=ActionType.SELL,
                        price=sell_price,
                        shares=position,
                        timestamp=current_date,
                        commission=commission,
                    )
                )
                position = 0

            # 记录权益
            equity_history.append(cash + position * current_price)

        # 最终估值
        final_price = float(df["close"].iloc[-1])
        final_cash = cash + position * final_price

        # 计算指标
        metrics = self._calculate_metrics(equity_history, trades)

        # 交易分析
        trade_analysis = self._analyze_trades(trades)

        # 基准对比
        benchmark = self._calc_benchmark(equity_history, df)

        result = BacktestResult(
            strategy_name=strategy.name,
            symbol=symbol,
            initial_cash=self.initial_cash,
            final_cash=final_cash,
            trades=trades,
            equity_curve=pd.Series(equity_history),
            metrics=metrics,
            trade_analysis=trade_analysis,
            benchmark=benchmark,
        )

        logger.info(
            f"回测完成: {strategy.name} / {symbol}, "
            f"收益率={result.total_return:.2%}, 交易={len(trades)}笔, "
            f"胜率={trade_analysis.win_rate:.2%}, Alpha={benchmark.alpha:.2%}"
        )
        return result

    def _calculate_metrics(
        self, equity_history: list[float], trades: list[Trade]
    ) -> dict[str, float]:
        """计算回测绩效指标"""
        metrics: dict[str, float] = {}

        if not equity_history or len(equity_history) < 2:
            metrics["total_return"] = 0.0
            return metrics

        equity = np.array(equity_history)
        initial = equity[0]
        final = equity[-1]

        # 总收益率
        metrics["total_return"] = (final - initial) / initial if initial != 0 else 0.0

        # 年化收益率(假设250个交易日)
        n_days = len(equity)
        if n_days > 1 and final > 0 and initial > 0:
            metrics["annual_return"] = (final / initial) ** (250 / n_days) - 1
        else:
            metrics["annual_return"] = 0.0

        # 日收益率
        daily_returns = np.diff(equity) / equity[:-1]
        daily_returns = daily_returns[np.isfinite(daily_returns)]

        if len(daily_returns) > 0:
            # 夏普比率(无风险利率=0)
            std = np.std(daily_returns)
            metrics["sharpe"] = np.mean(daily_returns) / std * np.sqrt(250) if std > 0 else 0.0

            # 索提诺比率
            downside = daily_returns[daily_returns < 0]
            downside_std = np.std(downside) if len(downside) > 0 else 0.0
            metrics["sortino"] = (
                np.mean(daily_returns) / downside_std * np.sqrt(250) if downside_std > 0 else 0.0
            )

            # 最大回撤
            peak = np.maximum.accumulate(equity)
            drawdown = (equity - peak) / peak
            metrics["max_drawdown"] = float(np.min(drawdown))

            # 波动率
            metrics["volatility"] = float(std * np.sqrt(250))

            # Calmar比率(年化收益/最大回撤绝对值)
            max_dd_abs = abs(metrics["max_drawdown"])
            if max_dd_abs > 0:
                metrics["calmar"] = metrics.get("annual_return", 0.0) / max_dd_abs
            else:
                metrics["calmar"] = 0.0

            # 最大回撤持续期(天数)
            metrics["max_drawdown_duration"] = self._calc_max_drawdown_duration(equity)
        else:
            metrics["sharpe"] = 0.0
            metrics["sortino"] = 0.0
            metrics["max_drawdown"] = 0.0
            metrics["volatility"] = 0.0
            metrics["calmar"] = 0.0
            metrics["max_drawdown_duration"] = 0.0

        # 交易统计
        if trades:
            buy_trades = [t for t in trades if t.action == ActionType.BUY]
            sell_trades = [t for t in trades if t.action == ActionType.SELL]

            metrics["total_trades"] = len(trades)
            metrics["buy_count"] = len(buy_trades)
            metrics["sell_count"] = len(sell_trades)

            # 胜率(配对买卖)
            paired_returns = []
            for i in range(min(len(buy_trades), len(sell_trades))):
                ret = (sell_trades[i].price - buy_trades[i].price) / buy_trades[i].price
                paired_returns.append(ret)

            if paired_returns:
                metrics["win_rate"] = sum(1 for r in paired_returns if r > 0) / len(paired_returns)
                avg_win = (
                    np.mean([r for r in paired_returns if r > 0])
                    if any(r > 0 for r in paired_returns)
                    else 0.0
                )
                avg_loss = (
                    abs(np.mean([r for r in paired_returns if r <= 0]))
                    if any(r <= 0 for r in paired_returns)
                    else 0.0
                )
                metrics["profit_loss_ratio"] = avg_win / avg_loss if avg_loss > 0 else float("inf")
            else:
                metrics["win_rate"] = 0.0
                metrics["profit_loss_ratio"] = 0.0

            # 总佣金
            metrics["total_commission"] = sum(t.commission for t in trades)
        else:
            metrics["total_trades"] = 0
            metrics["win_rate"] = 0.0

        return metrics

    @staticmethod
    def _calc_max_drawdown_duration(equity: np.ndarray) -> float:
        """计算最大回撤持续期(天数)"""
        peak = np.maximum.accumulate(equity)
        in_drawdown = equity < peak
        if not np.any(in_drawdown):
            return 0.0

        max_duration = 0
        current_duration = 0
        for is_dd in in_drawdown:
            if is_dd:
                current_duration += 1
                max_duration = max(max_duration, current_duration)
            else:
                current_duration = 0
        return float(max_duration)

    def _analyze_trades(self, trades: list[Trade]) -> TradeAnalysis:
        """分析配对交易，生成交易分析报告"""
        analysis = TradeAnalysis()

        buy_trades = [t for t in trades if t.action == ActionType.BUY]
        sell_trades = [t for t in trades if t.action == ActionType.SELL]

        n_pairs = min(len(buy_trades), len(sell_trades))
        if n_pairs == 0:
            return analysis

        pairs = []
        for i in range(n_pairs):
            pair = TradePair(buy_trade=buy_trades[i], sell_trade=sell_trades[i])
            pairs.append(pair)

        returns = [p.return_pct for p in pairs]
        wins = [p for p in pairs if p.is_win]
        losses = [p for p in pairs if not p.is_win]

        analysis.total_pairs = n_pairs
        analysis.win_count = len(wins)
        analysis.loss_count = len(losses)
        analysis.win_rate = len(wins) / n_pairs if n_pairs > 0 else 0.0
        analysis.avg_return = float(np.mean(returns))
        analysis.avg_win_return = float(np.mean([p.return_pct for p in wins])) if wins else 0.0
        analysis.avg_loss_return = float(np.mean([p.return_pct for p in losses])) if losses else 0.0
        analysis.best_trade_return = max(returns) if returns else 0.0
        analysis.worst_trade_return = min(returns) if returns else 0.0
        analysis.avg_holding_days = float(np.mean([p.holding_days for p in pairs]))

        # 连续胜负
        max_wins = 0
        max_losses = 0
        current_wins = 0
        current_losses = 0
        for p in pairs:
            if p.is_win:
                current_wins += 1
                current_losses = 0
                max_wins = max(max_wins, current_wins)
            else:
                current_losses += 1
                current_wins = 0
                max_losses = max(max_losses, current_losses)

        analysis.max_consecutive_wins = max_wins
        analysis.max_consecutive_losses = max_losses

        # 盈利因子(总盈利/总亏损绝对值)
        total_profit = sum(p.profit for p in wins)
        total_loss = abs(sum(p.profit for p in losses))
        analysis.total_profit = total_profit
        analysis.total_loss = total_loss
        analysis.profit_factor = total_profit / total_loss if total_loss > 0 else float("inf")

        return analysis

    def _calc_benchmark(self, equity_history: list[float], df: pd.DataFrame) -> BenchmarkResult:
        """计算买入持有基准对比"""
        result = BenchmarkResult()

        if len(equity_history) < 2 or len(df) < 2:
            return result

        equity = np.array(equity_history)
        strategy_return = (equity[-1] - equity[0]) / equity[0] if equity[0] != 0 else 0.0

        # 买入持有基准收益
        prices = df["close"].values
        window = min(60, len(df) - 1)
        benchmark_start = float(prices[window])
        benchmark_end = float(prices[-1])
        benchmark_return = (
            (benchmark_end - benchmark_start) / benchmark_start if benchmark_start != 0 else 0.0
        )

        result.strategy_return = strategy_return
        result.benchmark_return = benchmark_return
        result.alpha = strategy_return - benchmark_return

        # Beta: 策略日收益与基准日收益的协方差/基准方差
        if len(equity) > 2 and len(prices) > window + 2:
            strat_returns = np.diff(equity) / equity[:-1]
            bench_prices = prices[window:]
            bench_returns = np.diff(bench_prices.astype(float)) / bench_prices[:-1].astype(float)
            min_len = min(len(strat_returns), len(bench_returns))
            if min_len > 1:
                strat_returns = strat_returns[:min_len]
                bench_returns = bench_returns[:min_len]
                bench_var = np.var(bench_returns)
                if bench_var > 0:
                    result.beta = float(np.cov(strat_returns, bench_returns)[0, 1] / bench_var)

                # 信息比率
                excess_returns = strat_returns - bench_returns
                tracking_error = float(np.std(excess_returns)) * np.sqrt(252)
                result.tracking_error = tracking_error
                if tracking_error > 0:
                    result.information_ratio = float(np.mean(excess_returns)) * 252 / tracking_error

        return result

    def compare_strategies(
        self,
        strategies: list[BaseStrategy],
        df: pd.DataFrame,
        symbol: str = "test",
    ) -> dict[str, BacktestResult]:
        """比较多个策略的回测结果

        Args:
            strategies: 策略实例列表
            df: 历史行情数据
            symbol: 股票代码

        Returns:
            策略名→回测结果的映射
        """
        results: dict[str, BacktestResult] = {}
        for strategy in strategies:
            try:
                result = self.run(strategy, df, symbol)
                results[strategy.name] = result
                logger.debug(f"策略比较: {strategy.name} 完成")
            except Exception as e:
                logger.warning(f"策略比较: {strategy.name} 失败: {e}")
        return results


@dataclass
class StrategyPerformance:
    """策略绩效"""

    strategy_name: str
    total_signals: int = 0
    buy_signals: int = 0
    sell_signals: int = 0
    hold_signals: int = 0
    avg_confidence: float = 0.0
    results: list[StrategyResult] = field(default_factory=list)


class StrategyEngine:
    """策略引擎

    管理多个策略的注册、执行和信号聚合。

    使用示例:
        engine = StrategyEngine()
        engine.register(ManualStrategy())
        engine.register(MyQuantStrategy())

        results = engine.run_all("000001", df)
        print(engine.get_performance())
    """

    def __init__(self):
        self._strategies: dict[str, BaseStrategy] = {}
        self._performances: dict[str, StrategyPerformance] = {}
        self._weights: dict[str, float] = {}  # 策略权重

    def register(self, strategy: BaseStrategy, weight: float = 1.0) -> None:
        """注册策略

        Args:
            strategy: 策略实例
            weight: 策略权重(用于信号聚合)
        """
        self._strategies[strategy.name] = strategy
        self._weights[strategy.name] = weight
        self._performances[strategy.name] = StrategyPerformance(strategy_name=strategy.name)
        logger.info(f"注册策略: {strategy.name} (权重={weight})")

    def unregister(self, name: str) -> None:
        """注销策略"""
        if name in self._strategies:
            del self._strategies[name]
            del self._weights[name]
            del self._performances[name]
            logger.info(f"注销策略: {name}")

    def run_all(self, symbol: str, df: pd.DataFrame) -> dict[str, StrategyResult]:
        """执行所有策略

        Args:
            symbol: 股票代码
            df: 行情数据

        Returns:
            各策略执行结果
        """
        results = {}
        for name, strategy in self._strategies.items():
            try:
                result = strategy.run(symbol, df)
                results[name] = result
                self._update_performance(name, result)
            except Exception as e:
                logger.error(f"策略 {name} 执行失败: {e}")
        return results

    def aggregate_signal(self, results: dict[str, StrategyResult]) -> StrategyResult:
        """聚合多策略信号(加权投票)

        Args:
            results: 各策略执行结果

        Returns:
            聚合后的策略结果
        """
        if not results:
            return StrategyResult(symbol="", action=ActionType.HOLD, confidence=0.0)

        # 加权投票
        buy_score = 0.0
        sell_score = 0.0
        hold_score = 0.0
        total_weight = 0.0
        symbol = ""

        for name, result in results.items():
            weight = self._weights.get(name, 1.0)
            confidence = result.confidence
            symbol = result.symbol

            if result.action == ActionType.BUY:
                buy_score += weight * confidence
            elif result.action == ActionType.SELL:
                sell_score += weight * confidence
            else:
                hold_score += weight * confidence

            total_weight += weight

        if total_weight == 0:
            return StrategyResult(symbol=symbol, action=ActionType.HOLD, confidence=0.0)

        # 归一化
        buy_score /= total_weight
        sell_score /= total_weight
        hold_score /= total_weight

        # 选择得分最高的动作
        scores = {
            ActionType.BUY: buy_score,
            ActionType.SELL: sell_score,
            ActionType.HOLD: hold_score,
        }
        best_action = max(scores, key=scores.get)
        best_confidence = scores[best_action]

        return StrategyResult(
            symbol=symbol,
            action=best_action,
            confidence=best_confidence,
            reason=f"聚合信号(buy={buy_score:.2f}, sell={sell_score:.2f}, hold={hold_score:.2f})",
            metadata={"individual_results": {n: str(r) for n, r in results.items()}},
        )

    def get_performance(self) -> dict[str, StrategyPerformance]:
        """获取所有策略绩效"""
        return self._performances.copy()

    def list_strategies(self) -> list[str]:
        """列出已注册策略"""
        return list(self._strategies.keys())

    def _update_performance(self, name: str, result: StrategyResult) -> None:
        """更新策略绩效"""
        perf = self._performances[name]
        perf.total_signals += 1
        perf.results.append(result)

        if result.action == ActionType.BUY:
            perf.buy_signals += 1
        elif result.action == ActionType.SELL:
            perf.sell_signals += 1
        else:
            perf.hold_signals += 1

        # 更新平均信心度
        perf.avg_confidence = (
            perf.avg_confidence * (perf.total_signals - 1) + result.confidence
        ) / perf.total_signals
