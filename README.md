# Stock Model - 股票分析与投资模型

> 第一期：手动分析工具 ✅ | 第二期：自动量化Agent ✅

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
│   ├── visualization/         # 可视化层
│   │   └── charts.py          # 图表 (Plotly)
│   ├── web/                   # 🆕 Web Dashboard
│   │   └── app.py             # FastAPI仪表盘
│   └── utils/                 # 工具
│       ├── logger.py          # 日志 (loguru)
│       └── helpers.py         # 辅助函数
├── examples/                  # 示例脚本
├── tests/                     # 测试 (185个)
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
pip install -e ".[ta]"       # 技术分析 (pandas-ta, ta-lib)
pip install -e ".[schedule]" # 定时采集 (apscheduler)
pip install -e ".[web]"      # Web Dashboard (fastapi, uvicorn)
pip install -e ".[rl]"       # 强化学习 (stable-baselines3, torch)
```

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

- **FastAPI应用**: 仪表盘 + REST API
  - 健康检查、信号CRUD、回测执行
  - 组合优化、数据质量检查
  - 内嵌HTML仪表盘 (实时刷新)

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
pytest tests/test_data_sources.py -v    # 数据源
pytest tests/test_strategy_engine.py -v # 策略引擎
pytest tests/test_risk.py -v            # 风险管理
pytest tests/test_batch2.py -v          # 数据采集+信号推送
pytest tests/test_batch3.py -v          # 组合优化+RL+Dashboard
```

当前共 **185** 个单元测试，覆盖全部模块。

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