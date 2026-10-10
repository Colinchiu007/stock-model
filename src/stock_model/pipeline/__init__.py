"""
自动化交易流水线

编排完整交易流程: 数据采集 → 质量检查 → 技术分析 → 信号生成 → 策略执行 → 风控 → 仓位 → 推送
"""

from stock_model.pipeline.config import PipelineConfig
from stock_model.pipeline.models import PipelineResult, PipelineStatus
from stock_model.pipeline.trading_pipeline import TradingPipeline

__all__ = ["PipelineConfig", "PipelineResult", "PipelineStatus", "TradingPipeline"]
