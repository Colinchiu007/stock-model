# Stock Model - 股票分析与投资模型

> 第一期：手动分析工具 ✅ | 第二期：自动量化Agent ✅

## ⚠️ 先读这个：策略特性与局限

**内置的 `ManualStrategy` 是抗跌型择时策略，不是赚钱型选股策略。**

用真实数据（1 万元模拟盘，动态分散股票池，按市场环境分组）实测：

| 场景 | 成交 | 策略 | 基准（买入持有） | 超额 |
|------|-----:|-----:|-----:|-----:|
| **上涨市（2019）** | 23 笔 | **-1.26%** | **+40.07%** | **-41.32%** ❌ |
| 下跌市（2021） | 22 笔 | +8.73% | -24.79% | +33.52% |
| 下跌市（2024） | 23 笔 | +14.76% | -28.47% | +43.24% |

- **上涨市跑输 41 个百分点，且是亏损不是踏空** —— 换用分散股票池后
  交易次数翻了 5 倍，表现反而更差，确认是**策略方向问题**而非选股问题
- **下跌的超额主要来自"市场跌得更多"**，不是选股能力
- ❌ **趋势过滤器已实测无效并默认关闭**：曾按「67% 卖出发生在上涨趋势内」实现
  逆势拦截，实测上涨市平均 -2.36pp、跌市平均 -4.11pp，**净效果为负**。
  代码保留（`use_trend_filter=True` 可启用）但**不建议启用**，详见评估报告
- 📌 结论：**这是一个震荡市策略，在单边市里会被反复反噬。**
  想改善涨市表现需提高信号质量或换趋势跟随型策略，而非加过滤规则

**完整评估、根因数据与负面实验记录见 [`docs/strategy-evaluation-2026-10-07.md`](docs/strategy-evaluation-2026-10-07.md)。**

> 💡 **项目当前无全市场选股能力** —— 股票池由调用方指定。
> 这是"超额收益不可信"的根本原因，也是最高优先级的待改进项。

## 项目架构

```
stock-model/
├── config/                    # 配置文件
├── data/                      # 数据存储
│   ├── raw/                   # 原始数据
│   ├── processed/             # 处理后数据
│   └── cache/                 # TTL文件缓存
├── src/stock_model/           # 核心代码
│   ├── config/                # 配置管理 (pydantic-settings)
│   ├── data/                  # 数据层
│   │   ├── fetcher.py         # 数据获取器 (多源自动降级)
│   │   ├── sources/           # 数据源实现
│   │   │   ├── base.py        # 数据源抽象基类
│   │   │   ├── akshare_source.py  # Akshare数据源 (主源)
│   │   │   └── baostock_source.py # Baostock数据源 (备用)
│   │   ├── collector.py       # 🆕 自动数据采集器
│   │   ├── monitor.py         # 🆕 数据质量监控器
│   │   ├── processor.py       # 数据处理
│   │   └── storage.py         # 数据存储
│   ├── analysis/              # 分析层
│   │   ├── technical.py       # 技术分析 (MA/MACD/RSI/KDJ/BOLL)
│   │   ├── indicators.py      # 纯pandas指标实现 (pandas-ta缺失时兜底)
│   │   ├── fundamental.py     # 基本面分析 (PE/PB/ROE)
│   │   └── signals.py         # 信号生成
│   ├── strategy/              # 策略层
│   │   ├── base.py            # 策略基类
│   │   ├── manual.py          # 手动策略
│   │   ├── quant_strategy.py  # 🆕 量化策略基类 (参数化+网格优化)
│   │   ├── engine.py          # 🆕 策略引擎+回测引擎
│   │   ├── trading_env.py     # 🆕 Gymnasium交易环境
│   │   └── rl_agent.py        # 🆕 RL交易Agent (PPO/DQN)
│   ├── risk/                  # 🆕 风险管理
│   │   ├── models.py          # 风险数据模型
│   │   ├── manager.py         # 风险管理器 (回撤/集中度/止损)
│   │   └── position_sizer.py  # 仓位管理 (凯利/风险平价/ATR)
│   ├── portfolio/             # 🆕 组合优化
│   │   ├── models.py          # 组合数据模型
│   │   └── optimizer.py       # 组合优化器 (4种方法)
│   ├── notify/                # 🆕 信号推送
│   │   ├── notifier.py        # 信号通知器
│   │   └── channels.py        # 推送通道 (控制台/文件/Webhook)
│   ├── pipeline/              # 🆕 交易流水线
│   │   ├── config.py          # 流水线配置 (YAML支持)
│   │   ├── models.py          # 执行结果/状态模型
│   │   └── trading_pipeline.py # 8步编排 (采集→质量→分析→策略→风控→仓位→推送)
│   ├── visualization/         # 可视化层
│   │   └── charts.py          # 图表 (Plotly)
│   ├── web/                   # 🆕 Web Dashboard
│   │   ├── app.py             # FastAPI应用 (Pipeline控制/SSE)
│   │   ├── screener.py        # 🆕 选股/分析/回测 API
│   │   └── static/            # 🆕 前端资源 (html/css/js)
│   └── utils/                 # 工具
│       ├── logger.py          # 日志 (loguru)
│       └── helpers.py         # 辅助函数
├── examples/                  # 示例脚本
├── tests/                     # 测试 (591个)
├── docs/                      # 架构/PRD/复盘文档
├── .github/workflows/         # CI/CD (GitHub Actions)
└── pyproject.toml             # 项目配置
```

## 快速开始

### 安装

```bash
# 创建虚拟环境
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
# .venv\Scripts\activate   # Windows

# 基础安装
pip install -e ".[dev]"

# 可选依赖
pip install -e ".[quant]"    # 组合优化 (scipy)
pip install -e ".[schedule]" # 定时采集 (apscheduler)
pip install -e ".[web]"      # Web Dashboard (fastapi, uvicorn)
pip install -e ".[rl]"       # 强化学习 (stable-baselines3, torch)

# 技术分析加速 (可选)
# ⚠️ pandas-ta 仅支持 Python >= 3.12，在 3.10/3.11 上装不上。
# 不装也能用 —— analysis/indicators.py 提供了纯 pandas 的
# MACD/RSI/BOLL/ATR/KDJ/OBV 实现，列名与 pandas-ta 对齐。
# 装它只是数值口径可能与看盘软件更接近，不装则完全可用。
pip install pandas-ta        # Python >= 3.12 only
```

### 启动 Web 仪表盘

```bash
pip install -e ".[web]"
uvicorn --app-dir src "stock_model.web.app:create_app" --factory --port 8000
# 打开 http://localhost:8000
```

四个标签页：**选股榜**（批量扫描，按信号评分排序）、**个股分析**（指标快照 +
触发信号 + 60日走势）、**回测 · 交易记录**（逐笔买卖 + 绩效 + 权益曲线）、
**Pipeline**（定时任务启停与执行历史）。

### 使用示例

```python
from stock_model.data.fetcher import StockDataFetcher
from stock_model.analysis.technical import TechnicalAnalysis
from stock_model.analysis.signals import SignalGenerator
from stock_model.strategy.manual import ManualStrategy

# 1. 获取数据 (自动降级: akshare → baostock)
fetcher = StockDataFetcher()
df = fetcher.get_daily("000001", start_date="20240101")

# 2. 技术分析
ta = TechnicalAnalysis()
df = ta.analyze_all(df)

# 3. 生成信号
signals = SignalGenerator()
trading_signals = signals.generate_all(df, "000001")

# 4. 策略分析
strategy = ManualStrategy()
result = strategy.run("000001", df)
print(f"操作建议: {result.action.value}, 信心度: {result.confidence:.2f}")

# 5. 估值数据
valuation = fetcher.get_valuation("000001")
print(valuation[["pe_ttm", "pb", "ps_ttm"]].tail())

# 6. 财务摘要
financial = fetcher.get_financial_summary("000001")
print(financial.head())
```

### 🆕 二期功能示例

```python
# === 量化策略 + 回测 ===
from stock_model.strategy.engine import BacktestEngine, StrategyEngine
from stock_model.strategy.manual import ManualStrategy

engine = BacktestEngine(initial_cash=100000)
result = engine.run(ManualStrategy(), df, "000001")
print(f"总收益率: {result.metrics['total_return']:.2%}")
print(f"夏普比率: {result.metrics['sharpe']:.2f}")
print(f"最大回撤: {result.metrics['max_drawdown']:.2%}")

# === 风险管理 ===
from stock_model.risk import RiskManager, PositionSizer

risk_mgr = RiskManager(max_drawdown=0.15, max_concentration=0.3)
alerts = risk_mgr.check_portfolio_risk(positions, portfolio_value)

sizer = PositionSizer()
size = sizer.kelly_size(win_rate=0.55, win_loss_ratio=2.0, capital=100000)

# === 组合优化 ===
from stock_model.portfolio import PortfolioOptimizer

optimizer = PortfolioOptimizer()
portfolio = optimizer.equal_weight(["000001", "000002", "000003"], prices, 100000)
portfolio = optimizer.risk_parity(returns, prices, 100000)
portfolio = optimizer.min_variance(returns, prices, 100000)

# === 数据质量监控 ===
from stock_model.data.monitor import DataQualityMonitor

monitor = DataQualityMonitor()
report = monitor.check(df, "000001")
print(f"数据质量评分: {report['score']}/100")

# === 信号推送 ===
from stock_model.notify import SignalNotifier

notifier = SignalNotifier()
notifier.add_channel(ConsoleChannel())
notifier.add_channel(FileChannel("signals.json"))
notifier.notify(result)

# === RL Agent ===
from stock_model.strategy import RLTradingAgent

agent = RLTradingAgent(model_type="ppo")
result = agent.analyze("000001", df)  # 降级为规则策略
# agent.train(env, timesteps=10000)  # 需要stable-baselines3
```

### 运行演示

```bash
python examples/demo.py
```

## 核心模块

### 数据层 (`data/`)

- **DataFetcher**: 数据获取器，支持多数据源自动降级
  - **主源**: Akshare (东方财富/同花顺)
  - **备用**: Baostock (证券宝，独立服务器)
  - 自动降级: 主源连接失败时自动切换备用源
  - 日线/周线/月线行情、实时行情
  - 估值数据 (PE/PB/PS/股息率/市值)、财务摘要
- **🆕 AutoDataCollector**: 自动数据采集器
  - 监控列表管理、回调通知、APScheduler定时采集
- **🆕 DataQualityMonitor**: 数据质量监控
  - 缺失值检测、Z-score异常检测、数据新鲜度、Schema验证
  - 综合质量评分 (0-100)
- **TTL缓存**: 基于文件修改时间的过期检查

### 策略层 (`strategy/`)

- **BaseStrategy**: 策略基类 (统一接口)
- **ManualStrategy**: 手动综合策略
- **🆕 QuantStrategy**: 量化策略基类
  - 参数化策略 (StrategyParams)
  - 网格搜索优化 (笛卡尔积参数组合)
- **🆕 BacktestEngine**: 回测引擎
  - 滚动窗口回测、滑点/佣金模拟
  - 绩效指标: 总收益率/年化/夏普/索提诺/最大回撤/胜率/盈亏比
- **🆕 StrategyEngine**: 多策略聚合引擎
  - 策略注册/注销、加权投票信号聚合、绩效追踪
- **🆕 RLTradingAgent**: 强化学习交易Agent
  - PPO/DQN模型 (需stable-baselines3)
  - 降级为均线规则策略

### 🆕 风险管理 (`risk/`)

- **RiskManager**: 风险管理器
  - 多级回撤警报 (50%/80%/100%阈值)
  - 集中度检查、止损/止盈触发
- **PositionSizer**: 仓位管理
  - 固定比例、凯利公式 (半凯利)
  - 风险平价 (波动率倒数)、ATR仓位

### 🆕 组合优化 (`portfolio/`)

- **PortfolioOptimizer**: 组合优化器
  - 等权重 (1/N)
  - 风险平价 (波动率倒数加权)
  - 最小方差 (scipy.optimize，无scipy降级)
  - 均值方差 (Markowitz，最大夏普/目标收益)

### 🆕 信号推送 (`notify/`)

- **SignalNotifier**: 信号通知器 (格式化+多通道分发)
- **ConsoleChannel**: 控制台输出
- **FileChannel**: JSON行追加写入
- **WebhookChannel**: HTTP Webhook (钉钉/飞书/企微)

### 🆕 Web Dashboard (`web/`)

前端位于 `web/static/`（index.html + dashboard.css + dashboard.js），
由 FastAPI 通过 `StaticFiles` 挂载，不再内嵌在 Python 源码中。

**API 端点：**

| 端点 | 用途 |
|------|------|
| `GET /api/screener` | 批量扫描，返回按信号评分排序的**选股榜** |
| `GET /api/analyze/{symbol}` | 指标快照 + 触发信号 + 数据质量 + 60日走势 |
| `GET /api/backtest/{symbol}` | **逐笔买卖记录** + 绩效指标 + 权益曲线 |
| `GET /api/portfolio/optimize` | 组合优化 |
| `GET /api/quality/{symbol}` | 数据质量检查 |
| `POST /api/pipeline/{start,stop,run}` | 定时流水线控制 |
| `GET /api/events` | SSE 实时推送 |
| `GET /api/health` | 健康检查 |

交互式文档：启动服务后访问 `http://localhost:8000/docs`

### 🆕 技术指标 (`analysis/indicators.py`)

纯 pandas/numpy 实现的 MACD / RSI / BOLL / ATR / KDJ / OBV。

存在原因：`pandas-ta` 仅支持 Python >= 3.12，而项目支持 >= 3.10。
若无回退实现，在 3.10/3.11 上指标列会静默不产生，
导致信号生成器读不到值、策略对所有股票返回 hold（不报错、不告警）。

本实现的列名与 `pandas-ta` 严格对齐（`MACD_12_26_9` / `RSI_14` /
`BBL_20_2.0` / `atr14` / `kdj_k`），`technical.py` 会优先使用 pandas-ta，
缺失时自动回退到此处，上游代码无需改动。

## 配置

通过环境变量或 `.env` 文件配置：

```bash
# 数据源
STOCK_DATA_DEFAULT_SOURCE=akshare
STOCK_DATA_FALLBACK_SOURCE=baostock
STOCK_DATA_CACHE_DIR=data/cache
STOCK_DATA_CACHE_TTL=3600

# 分析参数
STOCK_ANALYSIS_MA_PERIODS=[5,10,20,60,120,250]

# 可视化
STOCK_VIZ_THEME=dark
STOCK_VIZ_EXPORT_FORMAT=html

# 网络代理 (可选)
STOCK_DATA_HTTP_PROXY=http://127.0.0.1:7890
STOCK_DATA_HTTPS_PROXY=http://127.0.0.1:7890
```

## 测试

```bash
# 运行全部测试
pytest tests/ -v

# 运行指定模块测试
pytest tests/test_data_sources.py -v        # 数据源
pytest tests/test_strategy_engine.py -v     # 策略引擎
pytest tests/test_risk.py -v                # 风险管理
pytest tests/test_trading_pipeline.py -v    # 交易流水线
pytest tests/test_indicators_and_screener.py -v  # 指标+选股API
pytest tests/test_bug_regressions.py -v     # 缺陷回归保护
pytest tests/test_parquet_fallback.py -v    # 缺可选依赖的降级路径
```

当前共 **591** 个测试（582 passed + 9 skipped），代码覆盖率 **81%**。

> **关于跳过的测试**：9 个 skip 均为「可选依赖未安装」类
> （如 pyarrow / fastapi 未装时相关用例跳过）。**skip 不代表通过**，
> 本项目已要求关键路径在缺依赖时降级而非跳过，并有对应测试锁定该行为。

## CI/CD

- **GitHub Actions**: 自动CI (push/PR触发)
  - Lint: Ruff check + format
  - Test: Python 3.10/3.11/3.12 矩阵测试 + 覆盖率
  - Build: 构建包 + twine 检查
- **分支保护**: main分支要求PR审查 + CI通过
- **Release**: `v*` tag自动构建 + GitHub Release

## 技术栈

| 类别 | 技术 |
|------|------|
| 语言 | Python 3.10+ |
| 数据获取 | akshare, baostock |
| 数据处理 | pandas, numpy |
| 技术分析 | pandas-ta, ta-lib (可选) |
| 量化优化 | scipy (可选) |
| 强化学习 | stable-baselines3, gymnasium (可选) |
| Web框架 | fastapi, uvicorn (可选) |
| 定时任务 | apscheduler (可选) |
| 可视化 | plotly, matplotlib |
| 配置 | pydantic-settings |
| 日志 | loguru |
| 测试 | pytest |
| CI/CD | GitHub Actions |

## License

MIT