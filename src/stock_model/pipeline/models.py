"""
流水线数据模型
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from stock_model.risk.models import RiskAlert
    from stock_model.strategy.base import StrategyResult


class PipelineStatus(str, Enum):
    """流水线执行状态"""

    EXECUTED = "executed"  # 正常执行
    SKIPPED = "skipped"  # 跳过(冷却期/数据不足)
    BLOCKED = "blocked"  # 风控拦截
    ERROR = "error"  # 执行异常


@dataclass
class PipelineResult:
    """流水线单次执行结果

    Attributes:
        symbol: 股票代码
        status: 执行状态
        strategy_result: 策略执行结果
        risk_alerts: 风险警报列表
        position_advice: 仓位建议 {"method": str, "shares": int, "capital": float}
        quality_score: 数据质量评分
        reason: 状态说明(跳过/拦截/异常原因)
        error: 异常信息
        timestamp: 执行时间
    """

    symbol: str
    status: PipelineStatus
    strategy_result: StrategyResult | None = None
    risk_alerts: list[RiskAlert] = field(default_factory=list)
    position_advice: dict[str, Any] | None = None
    quality_score: float = 0.0
    reason: str = ""
    error: str | None = None
    timestamp: datetime = field(default_factory=datetime.now)

    def __str__(self) -> str:
        parts = [f"[{self.status.value}] {self.symbol}"]
        if self.reason:
            parts.append(f"原因: {self.reason}")
        if self.strategy_result:
            parts.append(
                f"操作: {self.strategy_result.action.value} "
                f"信心度: {self.strategy_result.confidence:.0%}"
            )
        if self.position_advice:
            parts.append(f"建议仓位: {self.position_advice.get('shares', 0)}股")
        return " | ".join(parts)


@dataclass
class PipelineRunSummary:
    """流水线批量执行摘要

    Attributes:
        total: 总执行数
        executed: 正常执行数
        skipped: 跳过数
        blocked: 拦载数
        errors: 异常数
        results: 各股票执行结果
        run_time: 执行耗时(秒)
    """

    total: int = 0
    executed: int = 0
    skipped: int = 0
    blocked: int = 0
    errors: int = 0
    results: list[PipelineResult] = field(default_factory=list)
    run_time: float = 0.0

    @property
    def success_rate(self) -> float:
        """成功率"""
        if self.total == 0:
            return 0.0
        return self.executed / self.total

    def __str__(self) -> str:
        return (
            f"Pipeline摘要: 总计={self.total}, 执行={self.executed}, "
            f"跳过={self.skipped}, 拦截={self.blocked}, 异常={self.errors}, "
            f"成功率={self.success_rate:.0%}, 耗时={self.run_time:.1f}s"
        )
