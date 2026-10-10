# 二期架构规划：自动量化Agent

> 质量节拍 Phase 0→1 产出 | 全部7功能一起规划，分3批实现

## 交付批次

| 批次 | 功能 | 优先级 | 依赖 |
|------|------|--------|------|
| Batch 1 | 量化策略引擎 + 风险管理 | P0 | 一期策略层 |
| Batch 2 | 自动数据采集 + 信号推送 | P1 | Batch 1 |
| Batch 3 | 组合优化 + RL模型 + Dashboard | P2 | Batch 1+2 |

---

## Batch 1: 量化策略引擎 + 风险管理

### 1.1 量化策略引擎

**新增文件：**
- `strategy/quant_strategy.py` — QuantStrategy量化策略基类
- `strategy/engine.py` — StrategyEngine策略引擎 + BacktestEngine回测引擎

**QuantStrategy(BaseStrategy)：**
- `params: dict` — 策略参数(可优化)
- `optimize(param_grid, df)` — 参数优化(网格搜索)
- `backtest(df, initial_cash)` — 回测

**StrategyEngine：**
- `register(strategy)` — 注册策略
- `run_all(symbol, df)` — 执行所有策略并聚合信号(加权投票)
- `get_performance()` — 获取策略绩效

**BacktestEngine：**
- `run(strategy, df, initial_cash=100000)` — 运行回测
- `get_metrics()` — 回测指标(收益率/夏普/最大回撤/胜率/盈亏比)
- `get_trades()` — 交易记录
- `get_equity_curve()` — 权益曲线

**回测指标：**
- 总收益率、年化收益率、夏普比率、索提诺比率
- 最大回撤、最大回撤持续期、胜率、盈亏比
- 交易次数、平均持仓天数

### 1.2 风险管理

**新增目录：** `risk/`
- `risk/manager.py` — RiskManager风险管理器
- `risk/position_sizer.py` — PositionSizer仓位管理
- `risk/models.py` — 风险数据模型(RiskAlert/RiskLevel/RiskType)

**RiskManager：**
- `check_portfolio_risk(positions)` — 组合风险检查
- `check_position_risk(position)` — 单仓风险检查
- `max_drawdown_limit: float` — 最大回撤限制(默认15%)
- `position_concentration_limit: float` — 单股集中度(默认20%)
- `check_stop_loss(position)` — 止损检查
- `check_take_profit(position)` — 止盈检查

**PositionSizer：**
- `fixed_size(capital, price)` — 固定仓位
- `kelly_size(win_rate, avg_win, avg_loss)` — 凯利公式
- `risk_parity(volatilities)` — 风险平价
- `atr_size(capital, price, atr)` — ATR仓位

---

## Batch 2: 自动数据采集 + 信号推送

### 2.1 自动数据采集与监控

**新增文件：**
- `data/collector.py` — AutoDataCollector定时采集器
- `data/monitor.py` — DataQualityMonitor数据质量监控

**AutoDataCollector：**
- `add_watchlist(symbols)` — 添加监控列表
- `start(interval_minutes=30)` — 启动定时采集(APScheduler)
- `stop()` — 停止采集
- `collect_now()` — 立即采集
- `on_data(callback)` — 数据到达回调

**DataQualityMonitor：**
- `check_missing(df)` — 检查缺失数据
- `check_outlier(df)` — 检查异常值
- `check_staleness(df)` — 检查数据新鲜度
- `get_report()` — 质量报告

### 2.2 实时交易信号推送

**新增目录:** `notify/`
- `notify/notifier.py` — SignalNotifier信号通知器
- `notify/channels.py` — 推送通道(Console/File/Webhook)

**SignalNotifier：**
- `add_channel(channel)` — 添加推送通道
- `notify(signal, strategy_result)` — 发送通知

**推送通道：**
- ConsoleChannel — 控制台输出
- FileChannel — 写入文件(JSON/CSV)
- WebhookChannel — HTTP Webhook(钉钉/飞书/企微)

---

## Batch 3: 组合优化 + RL模型 + Dashboard

### 3.1 组合优化

**新增目录：** `portfolio/`
- `portfolio/optimizer.py` — PortfolioOptimizer
- `portfolio/models.py` — 组合数据模型

**PortfolioOptimizer：**
- `mean_variance(returns)` — 均值方差优化
- `risk_parity(returns)` — 风险平价
- `min_variance(returns)` — 最小方差
- `get_efficient_frontier(returns)` — 有效前沿

### 3.2 强化学习交易模型

**新增文件：** `strategy/rl_agent.py`
- `RLTradingAgent(BaseStrategy)` — RL交易Agent
- 状态空间: 行情+技术指标+持仓
- 动作空间: 买入/卖出/持有+仓位比例
- 奖励函数: 收益率-风险惩罚-交易成本
- 模型: PPO/DQN (依赖stable-baselines3)

### 3.3 Web Dashboard

**新增目录：** `web/`
- FastAPI + 静态前端
- API: 股票数据/策略执行/回测/组合概览
- WebSocket: 实时信号推送

---

## 新增依赖

```toml
[project.optional-dependencies]
quant = ["scipy>=1.11"]
web = ["fastapi>=0.100", "uvicorn", "websockets"]
schedule = ["apscheduler>=3.10"]
notify = ["httpx>=0.25"]
rl = ["stable-baselines3", "gymnasium"]
```

## 门禁决策记录

| 维度 | 判定 |
|------|------|
| 变更类型 | 📦 新增功能 |
| 变更规模 | 🌳 大型(跨模块5+文件) |
| 忿经门槛 | Phase 0.3 PRD → Phase 1→2 Test Plan |
| 路由Phase | Phase 0→1→2 |