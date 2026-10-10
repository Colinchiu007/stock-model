# 三期 PRD：自动化交易流水线 + Web Dashboard + RL Agent

> 质量节拍 Phase 0.3 产出 | v0.3.0→v0.4.0

## 版本规划

| 版本 | 功能 | 优先级 | 状态 |
|------|------|--------|------|
| v0.3.0 | TradingPipeline (P0) | P0 | ✅ 已发布 |
| v0.4.0 | Web Dashboard生产就绪 (P1) + RL Agent训练流程 (P2) + 性能优化 (P2) | P1/P2 | ✅ 已发布 |

---

## P0: TradingPipeline (v0.3.0)

### 功能需求

**FR-P0-01: 8步交易流水线编排**
- 数据采集 → 质量检查 → 技术分析 → 信号生成 → 策略执行 → 冷却检查 → 风控检查 → 仓位计算 → 信号推送
- 每步独立可配置，支持跳过和自定义

**FR-P0-02: 配置驱动**
- PipelineConfig 支持YAML配置
- watchlist, signal_cooldown, risk_params 等参数可配置

**FR-P0-03: 执行模式**
- `run_once(symbol)` — 单次执行
- `run_batch(symbols)` — 批量执行
- `start_scheduled(interval)` — 定时执行 (APScheduler)
- `stop()` — 停止定时任务

**FR-P0-04: 状态管理**
- PipelineStatus: IDLE/RUNNING/STOPPED/ERROR
- PipelineResult: 每次执行的完整结果
- PipelineRunSummary: 批量执行摘要

### 验收标准

- [x] TradingPipeline 可独立运行 run_once/run_batch
- [x] 配置驱动，PipelineConfig 可序列化
- [x] 定时执行可启停
- [x] 26个集成测试覆盖所有步骤
- [x] CI 6/6 checks passed

### 技术方案

- 状态机模式管理Pipeline生命周期
- 事件回调机制(on_start/on_complete/on_error)
- APScheduler集成定时执行
- 信号冷却期避免重复推送

---

## P1: Web Dashboard 生产就绪 (v0.4.0)

### 功能需求

**FR-P1-01: Pipeline控制API**
- `POST /api/pipeline/start` — 启动定时Pipeline
- `POST /api/pipeline/stop` — 停止Pipeline
- `POST /api/pipeline/run` — 单次执行Pipeline
- `GET /api/pipeline/status` — 获取Pipeline状态
- `GET /api/pipeline/history` — 获取执行历史

**FR-P1-02: SSE实时推送**
- `GET /api/events` — Server-Sent Events 实时推送
- 事件类型: pipeline_status, signal, backtest_result, error
- 心跳机制: 30秒超时
- asyncio.Queue 广播模式

**FR-P1-03: UI增强**
- Pipeline状态面板(运行/停止/错误)
- 控制按钮(启动/停止/单次执行)
- 执行历史列表(最近50条)
- 信号卡片 + 回测结果卡片
- SSE连接状态指示器

**FR-P1-04: 内存安全**
- deque(maxlen=200) — 信号历史
- deque(maxlen=50) — 执行历史
- deque(maxlen=100) — 回测结果
- 防止内存泄漏

### 验收标准

- [x] 5个Pipeline控制API端点可用
- [x] SSE实时推送正常工作
- [x] UI显示Pipeline状态和控制按钮
- [x] 内存使用有上限(deque限制)
- [x] ruff lint 0 errors, 测试通过（详见 docs/bug-reflection-2026-10-06.md）

### 技术方案

- FastAPI + Pydantic数据模型
- SSE via StreamingResponse + asyncio.Queue
- 闭包变量需nonlocal声明(F823修复)
- PipelineInstance引用管理(start/stop生命周期)

### 已知限制

- `_pipeline_instance`在多worker环境下不共享(内存状态)
- 生产环境需Redis/数据库替代内存状态
- SSE连接数受限于单进程

---

## P2: RL Agent 训练流程 (v0.4.0)

### 功能需求

**FR-P2-01: Gymnasium交易环境**
- TradingEnv兼容gymnasium.Env接口
- 观察空间: 9维特征向量
  - 归一化收盘价
  - 1日/5日/20日收益率
  - MA5/MA10/MA20偏离度
  - 20日波动率
  - RSI(14)
  - 量比(成交量/MA20成交量)
  - 价格位置(当前价/20日最高最低)
  - 均线斜率
- 动作空间: Discrete(3) — 0:HOLD, 1:BUY, 2:SELL
- 奖励函数: 持仓价值变化/初始资金 * 缩放因子 + 亏损>5%惩罚

**FR-P2-02: 交易模拟**
- 佣金: commission_rate (默认0.03%)
- 滑点: slippage (默认0.1%)
- 仓位限制: max_position_pct (默认0.95)
- 最小交易单位: 100股

**FR-P2-03: RL Agent训练**
- `train(env, timesteps, save_path, eval_env, eval_freq)` — 训练模型
- 支持 PPO/DQN 两种算法
- TrainingLogCallback — 训练进度日志
- EvalCallback — 定期评估+最优模型保存

**FR-P2-04: 模型持久化**
- `save(path)` — 保存模型(zip格式)
- `load(path)` — 加载模型
- `train_with_data(df, timesteps, save_path)` — 便捷训练方法

**FR-P2-05: 降级策略**
- stable-baselines3/gymnasium未安装时降级为规则策略
- is_trained/training_history属性

### 验收标准

- [x] TradingEnv兼容gymnasium.Env接口(reset/step/metadata)
- [x] 9维特征正确计算(归一化/收益率/MA/RSI/量比等)
- [x] 交易模拟含佣金+滑点
- [x] PPO/DQN训练可运行
- [x] 模型save/load正常工作
- [x] 未安装依赖时降级为规则策略
- [x] ruff lint 0 errors, 测试通过（详见 docs/bug-reflection-2026-10-06.md）

### 技术方案

- 延迟初始化observation_space/action_space(依赖数据维度)
- window_size步特征展平为观察向量
- NaN/Inf处理: 替换为0
- stable-baselines3 BaseCallback继承
- 规则策略降级: 基于RSI+MA的简单策略

### 已知限制

- 观察空间维度依赖数据列数，需确保特征一致性
- 单股票环境，不支持组合级训练
- 无超参数调优(Optuna集成待P3+)
- 奖励函数较简单，未考虑交易频率惩罚

---

## P2: 性能优化 (v0.4.0)

### 功能需求

**FR-P2-06: 批量执行日志降级**
- 批量执行路径15+处 INFO → DEBUG
- 保留Pipeline级别INFO日志(启动/停止/完成)
- 涉及模块: analysis(technical/signals), strategy(base), data(fetcher/monitor/processor)

### 验收标准

- [x] 批量执行路径日志降级为DEBUG
- [x] Pipeline关键节点保留INFO
- [x] 测试通过（详见 docs/bug-reflection-2026-10-06.md）

---

## 技术债务记录

| 编号 | 描述 | 优先级 | 状态 | 计划版本 |
|------|------|--------|------|----------|
| TD-01 | Web Dashboard多worker状态共享 | P3 | ⚠️ 已收口为「启动即报错」守卫<br>(真正共享需抽独立服务, 未做) | v0.5.0 |
| TD-02 | TradingEnv多股票环境 | P3 | ✅ 已实现 `MultiStockTradingEnv` | v0.5.0 |
| TD-03 | RL Agent超参数调优(Optuna) | P3 | 待办 | v0.5.0 |
| TD-04 | TradingEnv奖励函数增强 | P3 | ✅ 已实现 `BUILTIN_REWARDS` + 自定义 `RewardFn` | v0.5.0 |
| TD-05 | Web Dashboard用户认证 | P4 | 待办 | v0.6.0 |
| TD-06 | 实时行情WebSocket | P4 | 待办 | v0.6.0 |
| TD-07 | Docker化部署 | P4 | 待办 | v0.6.0 |

> **TD-01 说明**：多 worker 共享状态**未实现**，因为 pipeline 持有真实的
> APScheduler 后台线程，无法跨进程共享。用 Redis 只同步 status 反而更危险
> （界面显示"已停止"但调度器仍在跑）。当前做法是启动期检测到 `workers > 1`
> 直接抛 `MultiWorkerNotSupportedError`，把「未定义行为」变成「启动即报错」。
> 真正支持需将 pipeline 抽为独立单例服务，属架构级改造。

> **审查期间新增的缺陷**：2026-10-06/07 的质量审查共修复 **15 个静默缺陷**
> （含 2 个 P0），详见 `docs/bug-reflection-2026-10-06.md`。

---

## 变更影响分析

### 文件变更 (v0.4.0)

| 文件 | 变更类型 | 行数 |
|------|----------|------|
| `web/app.py` | 重写 | +648/-120 |
| `strategy/trading_env.py` | 新建 | +354 |
| `strategy/rl_agent.py` | 重写 | +219/-50 |
| `strategy/__init__.py` | 修改 | +2 |
| `__init__.py` | 修改 | +25/-5 |
| `analysis/technical.py` | 修改 | +4/-4 |
| `analysis/signals.py` | 修改 | +4/-4 |
| `strategy/base.py` | 修改 | +4/-4 |
| `data/fetcher.py` | 修改 | +20/-20 |
| `data/monitor.py` | 修改 | +2/-2 |
| `data/processor.py` | 修改 | +4/-4 |

### 向后兼容性

- ✅ 所有公共API保持兼容
- ✅ 新增功能均为可选依赖(gymnasium/stable-baselines3)
- ✅ 日志级别变更不影响功能
- ⚠️ Web Dashboard API v2.0→v3.0(新增端点，旧端点兼容)