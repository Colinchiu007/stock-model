"""
Stock Model - 股票分析与投资模型

Phase 1: 手动分析工具
  - 数据获取与处理 (多数据源: AkShare/BaoStock)
  - 技术分析指标 (MA/EMA/MACD/KDJ/RSI/ATR/Bollinger)
  - 基本面分析
  - 可视化报告
  - 手动策略

Phase 2: 自动量化Agent
  - 自动化交易策略 (Manual/Quant/RL Agent)
  - 策略引擎 (注册/执行/聚合/回测)
  - 风险管理 (止损/止盈/回撤/集中度)
  - 仓位计算 (固定/凯利/ATR/风险平价)
  - 信号推送 (多通道通知)
  - 组合优化 (等权/风险平价/最小方差/均值方差)

Phase 3: 自动化交易流水线
  - TradingPipeline (8步编排: 采集→质量→分析→策略→冷却→风控→仓位→推送)
  - PipelineConfig (配置驱动, YAML支持)
  - 定时执行 (APScheduler集成)
  - Web Dashboard (FastAPI)
"""

__version__ = "0.3.0"
