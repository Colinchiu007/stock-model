"""
自动化交易流水线

编排完整交易流程:
  数据采集 → 质量检查 → 技术分析 → 信号生成 → 策略执行 → 风控检查 → 仓位计算 → 信号推送

支持:
  - 单次执行 (run_once)
  - 批量执行 (run_batch)
  - 定时执行 (start_scheduled / stop)
  - 信号冷却期 (避免重复信号)
  - 配置驱动 (PipelineConfig)
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from loguru import logger

from stock_model.analysis.signals import SignalGenerator
from stock_model.analysis.technical import TechnicalAnalysis
from stock_model.data.collector import AutoDataCollector, MissingDependencyError
from stock_model.data.fetcher import StockDataFetcher
from stock_model.data.monitor import DataQualityMonitor
from stock_model.notify.channels import ConsoleChannel
from stock_model.notify.notifier import SignalNotifier
from stock_model.pipeline.config import PipelineConfig
from stock_model.pipeline.models import PipelineResult, PipelineRunSummary, PipelineStatus
from stock_model.risk.manager import RiskManager
from stock_model.risk.models import Position, RiskLevel
from stock_model.risk.position_sizer import PositionSizer
from stock_model.strategy.base import ActionType, BaseStrategy
from stock_model.strategy.engine import StrategyEngine

if TYPE_CHECKING:
    from collections.abc import Callable

    import pandas as pd


class TradingPipeline:
    """自动化交易流水线

    编排从数据采集到信号推送的完整交易流程，支持配置驱动和定时运行。

    使用示例:
        # 快速启动
        pipeline = TradingPipeline()
        pipeline.add_strategy(ManualStrategy())
        result = pipeline.run_once("000001")

        # 配置驱动
        config = PipelineConfig(watchlist=["000001", "600036"], signal_cooldown_minutes=30)
        pipeline = TradingPipeline(config)
        pipeline.add_strategy(ManualStrategy())
        summary = pipeline.run_batch(config.watchlist)

        # 定时运行
        pipeline.start_scheduled(interval_minutes=30)
        pipeline.stop()
    """

    def __init__(self, config: PipelineConfig | None = None):
        self.config = config or PipelineConfig()

        # 初始化组件
        self._fetcher = StockDataFetcher(source=self.config.data_source)
        self._collector = AutoDataCollector(fetcher=self._fetcher)
        self._quality_monitor = DataQualityMonitor()
        self._technical = TechnicalAnalysis()
        self._signal_gen = SignalGenerator()
        self._strategy_engine = StrategyEngine()
        self._risk_manager = RiskManager(
            max_drawdown_limit=self.config.risk_max_drawdown,
            position_concentration_limit=self.config.risk_position_concentration,
            default_stop_loss_pct=self.config.risk_stop_loss_pct,
            default_take_profit_pct=self.config.risk_take_profit_pct,
        )
        self._position_sizer = PositionSizer(
            max_position_pct=self.config.max_position_pct,
            min_shares=self.config.min_shares,
        )
        self._notifier = SignalNotifier()

        # 信号冷却期追踪: {symbol_action: datetime}
        self._signal_history: dict[str, datetime] = {}

        # 回调
        self._on_result_callbacks: list[Callable[[PipelineResult], None]] = []

        # 默认添加控制台通知
        if self.config.enable_notify:
            self._notifier.add_channel(ConsoleChannel())

    # ==================== 策略管理 ====================

    def add_strategy(self, strategy: BaseStrategy) -> None:
        """注册策略"""
        self._strategy_engine.register(strategy)
        logger.info(f"Pipeline注册策略: {strategy.name}")

    def remove_strategy(self, name: str) -> None:
        """移除策略"""
        self._strategy_engine.unregister(name)
        logger.info(f"Pipeline移除策略: {name}")

    # ==================== 通知管理 ====================

    def add_channel(self, channel) -> None:
        """添加通知通道"""
        self._notifier.add_channel(channel)

    def remove_channel(self, channel) -> None:
        """移除通知通道"""
        self._notifier.remove_channel(channel)

    # ==================== 回调 ====================

    def on_result(self, callback: Callable[[PipelineResult], None]) -> None:
        """注册结果回调(每次执行完成后触发)"""
        self._on_result_callbacks.append(callback)

    # ==================== 核心执行 ====================

    def run_once(self, symbol: str) -> PipelineResult:
        """对单只股票执行完整流水线

        流程: 数据采集 → 质量检查 → 技术分析 → 策略执行 → 冷却检查 → 风控 → 仓位 → 推送

        Args:
            symbol: 股票代码

        Returns:
            PipelineResult 执行结果
        """
        logger.info(f"Pipeline开始执行: {symbol}")
        start_time = time.time()

        try:
            # Step 1: 数据采集
            df = self._fetch_data(symbol)
            if df is None or len(df) < self.config.min_data_rows:
                return PipelineResult(
                    symbol=symbol,
                    status=PipelineStatus.SKIPPED,
                    reason=f"数据不足: {len(df) if df is not None else 0}行 "
                    f"(需要>={self.config.min_data_rows})",
                )

            # Step 2: 数据质量检查
            quality_score = 1.0
            if self.config.enable_quality_check:
                quality_score = self._check_quality(symbol, df)
                if quality_score < self.config.min_quality_score:
                    return PipelineResult(
                        symbol=symbol,
                        status=PipelineStatus.SKIPPED,
                        quality_score=quality_score,
                        reason=f"数据质量不达标: {quality_score:.2f} "
                        f"(需要>={self.config.min_quality_score})",
                    )

            # Step 3: 技术分析
            df = self._technical.analyze_all(df)

            # Step 4: 策略执行
            if not self._strategy_engine.list_strategies():
                return PipelineResult(
                    symbol=symbol,
                    status=PipelineStatus.SKIPPED,
                    quality_score=quality_score,
                    reason="未注册任何策略",
                )

            strategy_results = self._strategy_engine.run_all(symbol, df)
            strategy_result = self._strategy_engine.aggregate_signal(strategy_results)

            # Step 5: 信号冷却期检查
            if self._is_in_cooldown(symbol, strategy_result.action):
                return PipelineResult(
                    symbol=symbol,
                    status=PipelineStatus.SKIPPED,
                    strategy_result=strategy_result,
                    quality_score=quality_score,
                    reason=f"信号冷却中: {strategy_result.action.value}",
                )

            # Step 6: 风控检查
            risk_alerts: list = []
            if self.config.enable_risk_check and strategy_result.action != ActionType.HOLD:
                latest_price = float(df["close"].iloc[-1]) if len(df) > 0 else 0.0
                risk_alerts = self._check_risk(symbol, strategy_result, latest_price)
                critical_alerts = [a for a in risk_alerts if a.level == RiskLevel.CRITICAL]
                if critical_alerts:
                    self._record_signal(symbol, strategy_result.action)
                    return PipelineResult(
                        symbol=symbol,
                        status=PipelineStatus.BLOCKED,
                        strategy_result=strategy_result,
                        risk_alerts=risk_alerts,
                        quality_score=quality_score,
                        reason=f"风控拦截: {len(critical_alerts)}条严重警报",
                    )

            # Step 7: 仓位计算
            position_advice = None
            if strategy_result.action != ActionType.HOLD:
                position_advice = self._calculate_position(symbol, strategy_result, df)

            # Step 8: 信号推送
            if self.config.enable_notify and strategy_result.action != ActionType.HOLD:
                self._notifier.notify(strategy_result)

            # 记录信号时间
            self._record_signal(symbol, strategy_result.action)

            elapsed = time.time() - start_time
            logger.info(
                f"Pipeline完成: {symbol} | {strategy_result.action.value} | 耗时{elapsed:.2f}s"
            )

            result = PipelineResult(
                symbol=symbol,
                status=PipelineStatus.EXECUTED,
                strategy_result=strategy_result,
                risk_alerts=risk_alerts,
                position_advice=position_advice,
                quality_score=quality_score,
            )

        except (ValueError, KeyError, TypeError, RuntimeError) as e:
            logger.error(f"Pipeline执行异常: {symbol} | {e}")
            result = PipelineResult(
                symbol=symbol,
                status=PipelineStatus.ERROR,
                error=str(e),
                reason=f"执行异常: {e}",
            )

        # 触发回调
        for callback in self._on_result_callbacks:
            try:
                callback(result)
            except (ValueError, TypeError) as e:
                logger.warning(f"回调执行异常: {e}")

        return result

    def run_batch(self, symbols: list[str] | None = None) -> PipelineRunSummary:
        """批量执行流水线

        Args:
            symbols: 股票列表(为None时使用config.watchlist)

        Returns:
            PipelineRunSummary 批量执行摘要
        """
        symbols = symbols or self.config.watchlist
        start_time = time.time()
        results: list[PipelineResult] = []

        logger.info(f"Pipeline批量执行: {len(symbols)}只股票")

        for symbol in symbols:
            result = self.run_once(symbol)
            results.append(result)

        elapsed = time.time() - start_time

        summary = PipelineRunSummary(
            total=len(results),
            executed=sum(1 for r in results if r.status == PipelineStatus.EXECUTED),
            skipped=sum(1 for r in results if r.status == PipelineStatus.SKIPPED),
            blocked=sum(1 for r in results if r.status == PipelineStatus.BLOCKED),
            errors=sum(1 for r in results if r.status == PipelineStatus.ERROR),
            results=results,
            run_time=elapsed,
        )

        logger.info(str(summary))
        return summary

    # ==================== 定时执行 ====================

    def start_scheduled(self, interval_minutes: int = 30) -> None:
        """启动定时执行

        Args:
            interval_minutes: 采集间隔(分钟)

        Raises:
            MissingDependencyError: 调度器启动失败(如未安装 apscheduler)。

                底层 collector.start() 失败时必须向上传播,
                否则本方法会照常打印"已启动",让调用方与用户都误以为定时任务在跑。
        """
        self._collector.add_watchlist(self.config.watchlist)
        self._collector.on_data(self._on_data_callback)
        self._collector.start(interval_minutes=interval_minutes)

        if not self.is_running:
            # 防御: collector 返回了但实际未运行, 绝不能谎报成功
            raise MissingDependencyError("定时执行启动失败: 调度器未进入运行状态")

        logger.info(f"Pipeline定时执行已启动, 间隔{interval_minutes}分钟")

    def stop(self) -> None:
        """停止定时执行"""
        self._collector.stop()
        logger.info("Pipeline定时执行已停止")

    @property
    def is_running(self) -> bool:
        """是否正在定时执行"""
        return self._collector.is_running

    # ==================== 内部方法 ====================

    def _fetch_data(self, symbol: str) -> pd.DataFrame | None:
        """获取股票数据"""
        try:
            df = self._fetcher.get_daily(symbol, start_date=self.config.start_date)
            if df is not None:
                logger.debug(f"数据获取成功: {symbol}, {len(df)}行")
            return df
        except (ValueError, KeyError, ConnectionError, RuntimeError) as e:
            logger.error(f"数据获取失败: {symbol} | {e}")
            return None

    def _check_quality(self, symbol: str, df: pd.DataFrame) -> float:
        """数据质量检查, 返回评分(0-1)"""
        try:
            report = self._quality_monitor.check(df, symbol=symbol)
            score = report.score
            logger.debug(f"数据质量: {symbol} | 评分={score:.2f}")
            return score
        except (ValueError, KeyError, TypeError) as e:
            logger.warning(f"质量检查异常: {symbol} | {e}, 默认通过")
            return 1.0

    def _check_risk(self, symbol: str, strategy_result, current_price: float = 0.0) -> list:
        """风控检查

        用"拟建仓"视角评估策略建议: 以真实现价作为成本价, 策略给出的
        止损/止盈价作为阈值, 交给 RiskManager 判定是否需要拦截。

        注意 shares 必须非 0 —— RiskManager.check_position_risk 对
        shares<=0 的空仓直接短路返回, 若沿用占位的 0 会让风控形同虚设。

        Args:
            symbol: 股票代码
            strategy_result: 策略执行结果(提供 stop_loss/take_profit)
            current_price: 真实现价, 为 0 时回退到 strategy_result.target_price

        Returns:
            风险警报列表
        """
        try:
            price = current_price or strategy_result.target_price or 0.0
            if price <= 0:
                logger.debug(f"风控跳过: {symbol} 无有效价格")
                return []

            # 拟建仓: 以现价买入 1 手(100股)作为最小可评估单位
            # StrategyResult 目前无 take_profit 字段, 用 getattr 兼容未来扩展
            position = Position(
                symbol=symbol,
                shares=self.config.min_shares or 100,
                cost_price=price,
                current_price=price,
                stop_loss=getattr(strategy_result, "stop_loss", None),
                take_profit=getattr(strategy_result, "take_profit", None),
            )
            return self._risk_manager.check_position_risk(position)
        except (ValueError, KeyError, TypeError) as e:
            logger.warning(f"风控检查异常: {symbol} | {e}")
            return []

    def _calculate_position(self, symbol: str, strategy_result, df: pd.DataFrame) -> dict:
        """仓位计算"""
        try:
            current_price = strategy_result.target_price or (
                df["close"].iloc[-1] if len(df) > 0 else 0.0
            )
            capital = self.config.initial_capital

            if self.config.position_method == "kelly":
                # 凯利公式需要历史胜率数据，此处用固定比例兜底
                shares = self._position_sizer.fixed_size(
                    capital=capital,
                    price=current_price,
                    position_pct=self.config.position_fixed_pct,
                )
                method = "kelly(fallback=fixed)"
            elif self.config.position_method == "risk_parity":
                shares = self._position_sizer.fixed_size(
                    capital=capital,
                    price=current_price,
                    position_pct=self.config.position_fixed_pct,
                )
                method = "risk_parity(fallback=fixed)"
            else:
                shares = self._position_sizer.fixed_size(
                    capital=capital,
                    price=current_price,
                    position_pct=self.config.position_fixed_pct,
                )
                method = "fixed"

            return {
                "method": method,
                "shares": shares,
                "price": current_price,
                "capital": capital,
                "position_pct": self.config.position_fixed_pct,
            }
        except (ValueError, KeyError, ZeroDivisionError) as e:
            logger.warning(f"仓位计算异常: {symbol} | {e}")
            return {"method": "error", "shares": 0, "error": str(e)}

    def _is_in_cooldown(self, symbol: str, action: ActionType) -> bool:
        """检查信号是否在冷却期内"""
        if action == ActionType.HOLD:
            return False  # HOLD不触发冷却

        key = f"{symbol}_{action.value}"
        last_time = self._signal_history.get(key)
        if last_time is None:
            return False

        cooldown = timedelta(minutes=self.config.signal_cooldown_minutes)
        if datetime.now() - last_time < cooldown:
            logger.debug(f"信号冷却中: {key}, 上次触发={last_time}")
            return True
        return False

    def _record_signal(self, symbol: str, action: ActionType) -> None:
        """记录信号触发时间"""
        if action == ActionType.HOLD:
            return
        key = f"{symbol}_{action.value}"
        self._signal_history[key] = datetime.now()

    def _on_data_callback(self, symbol: str, df: pd.DataFrame) -> None:
        """数据采集回调(定时模式)"""
        logger.info(f"数据到达回调: {symbol}, {len(df)}行")
        self.run_once(symbol)

    # ==================== 状态查询 ====================

    @property
    def signal_history(self) -> dict[str, datetime]:
        """信号历史(只读副本)"""
        return self._signal_history.copy()

    def clear_signal_history(self) -> None:
        """清除信号历史(重置冷却期)"""
        self._signal_history.clear()
        logger.info("信号历史已清除")

    @property
    def stats(self) -> dict:
        """流水线统计"""
        return {
            "strategies": self._strategy_engine.list_strategies(),
            "watchlist": self.config.watchlist,
            "signal_history_count": len(self._signal_history),
            "is_running": self.is_running,
            "collector_stats": self._collector.stats,
        }
