# Stock Model - 股票分析与投资模型

> 第一期：手动分析工具 | 第二期：自动量化Agent (规划中)

## 项目架构

```
stock-model/
├── config/                    # 配置文件
├── data/                      # 数据存储
│   ├── raw/                   # 原始数据
│   ├── processed/             # 处理后数据
│   └── cache/                 # 缓存
├── src/stock_model/           # 核心代码
│   ├── config/                # 配置管理 (pydantic-settings)
│   ├── data/                  # 数据层
│   │   ├── fetcher.py         # 数据获取 (akshare)
│   │   ├── processor.py       # 数据处理
│   │   └── storage.py         # 数据存储
│   ├── analysis/              # 分析层
│   │   ├── technical.py       # 技术分析 (MA/MACD/RSI/KDJ/BOLL)
│   │   ├── fundamental.py     # 基本面分析 (PE/PB/ROE)
│   │   └── signals.py         # 信号生成
│   ├── visualization/        # 可视化层
│   │   └── charts.py          # 图表 (Plotly)
│   ├── strategy/              # 策略层
│   │   ├── base.py            # 策略基类
│   │   └── manual.py          # 手动策略
│   └── utils/                 # 工具
│       ├── logger.py          # 日志 (loguru)
│       └── helpers.py         # 辅助函数
├── examples/                  # 示例脚本
├── tests/                     # 测试
└── pyproject.toml             # 项目配置
```

## 快速开始

### 安装

```bash
# 创建虚拟环境
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
# .venv\Scripts\activate   # Windows

# 安装依赖
pip install -e ".[dev]"
```

### 使用示例

```python
from stock_model.data.fetcher import StockDataFetcher
from stock_model.analysis.technical import TechnicalAnalysis
from stock_model.analysis.signals import SignalGenerator
from stock_model.strategy.manual import ManualStrategy

# 1. 获取数据
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
```

### 运行演示

```bash
python examples/demo.py
```

## 核心模块

### 数据层 (`data/`)
- **StockDataFetcher**: 数据获取，支持 akshare (默认) / tushare
  - 日线/周线/月线行情
  - 实时行情
  - 财务数据、估值数据
  - 板块数据
- **DataProcessor**: 数据清洗、收益率计算、重采样
- **DataStorage**: CSV/Parquet 存储 + 缓存管理

### 分析层 (`analysis/`)
- **TechnicalAnalysis**: 技术分析
  - 趋势: MA/EMA/MACD
  - 动量: RSI/KDJ
  - 波动: BOLL/ATR
  - 量价: OBV/VOL_MA
- **FundamentalAnalysis**: 基本面分析
  - PE/PB/ROE 评分
  - 综合评分
- **SignalGenerator**: 交易信号生成
  - 均线金叉/死叉
  - MACD信号
  - RSI超买/超卖
  - 布林带信号

### 可视化层 (`visualization/`)
- **ChartBuilder**: 基于 Plotly 的图表
  - K线图 (含均线、成交量)
  - 技术指标图 (MACD/RSI/BOLL)
  - 导出 HTML/PNG/SVG/PDF

### 策略层 (`strategy/`)
- **BaseStrategy**: 策略基类 (统一接口)
- **ManualStrategy**: 手动综合策略
  - 技术信号统计
  - 趋势判断
  - 仓位建议
  - 目标价/止损价

## 配置

通过环境变量或 `.env` 文件配置:

```bash
# 数据源
STOCK_DATA_DEFAULT_SOURCE=akshare
STOCK_DATA_TUSHARE_TOKEN=your_token

# 分析参数
STOCK_ANALYSIS_MA_PERIODS=[5,10,20,60,120,250]

# 可视化
STOCK_VIZ_THEME=dark
STOCK_VIZ_EXPORT_FORMAT=html
```

## 第二期规划 (自动量化Agent)

- [ ] 自动化数据采集与监控
- [ ] 量化策略引擎 (基于 BaseStrategy 扩展)
- [ ] 强化学习交易模型
- [ ] 风险管理 Agent
- [ ] 组合优化
- [ ] 实时交易信号推送
- [ ] Web Dashboard

## 技术栈

| 类别 | 技术 |
|------|------|
| 语言 | Python 3.10+ |
| 数据获取 | akshare, tushare |
| 数据处理 | pandas, numpy |
| 技术分析 | pandas-ta, ta-lib |
| 可视化 | plotly, matplotlib |
| 配置 | pydantic-settings |
| 日志 | loguru |
| 测试 | pytest |

## License

MIT