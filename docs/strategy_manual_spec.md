# 策略规格：ManualStrategy（技术面综合打分）

> 本文是 `ManualStrategy` + `SignalGenerator` + `TechnicalAnalysis` 的**权威规格**，
> 所有数字与判据均从代码读出（2026-10-21 核对），非设计意图的描述。
> 代码位置：`src/stock_model/strategy/manual.py`、`src/stock_model/analysis/{signals,technical,indicators}.py`

## 0. 一句话概括

**逆势抄底策略**：要求价格已经跌了一波（RSI 超卖 / 触及布林下轨），
靠两个以上信号共振才买入；**不要求趋势转好**，`reason` 里会明确记录 `趋势=down`。
趋势过滤器本可拦截逆势操作，但**默认关闭**（实测净效果为负，见 §7）。

## 1. 数据流（五层，职责分离）

```
日线 OHLCV
  ↓ TechnicalAnalysis.analyze_all()          计算 33 个指标列
  ↓ SignalGenerator.generate_all()            4 条规则逐条产出 BUY/SELL 信号
  ↓ ManualStrategy.analyze()                 加权打分 → 决策 + 仓位 + 目标/止损
  ↓ PaperEngine                              按仓位算股数（含 20% 硬上限）
  ↓ Broker                                   T+1 用次日**开盘价**撮合
```

**信号层与决策层分离**：信号是"规则是否命中"，决策是"命中后怎么加权"。
改规则不影响打分，改打分不影响规则。

## 2. 指标算法

### 2.1 RSI（Wilder 平滑，period=14）

```
delta    = close.diff()
avg_gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
avg_loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
RSI      = 100 − 100 / (1 + avg_gain / avg_loss)
```

Wilder 平滑等价于 `alpha=1/N` 的指数加权，与 pandas-ta、主流行情软件一致。

**两个边界特判**（避免 0/0）：
| 情况 | 取值 |
|---|---|
| `avg_loss == 0`（全涨） | RSI = 100 |
| `avg_gain == avg_loss == 0`（横盘） | RSI = 50 |

### 2.2 布林带（period=20, std=2.0）

```
中轨 = close.rolling(20).mean()
上轨 = 中轨 + 2 × close.rolling(20).std()
下轨 = 中轨 − 2 × close.rolling(20).std()
```

标准差用 pandas `.std()` 默认 **ddof=1**（样本标准差，非总体）。

### 2.3 ATR（period=14）

```
TR  = max(high − low, |high − 前收|, |low − 前收|)
ATR = TR 的 14 日 Wilder 平滑
```

用于目标价/止损（§5）。

### 2.4 MACD（fast=12, slow=26, signal=9）

```
DIF   = EMA(close, 12) − EMA(close, 26)
DEA   = EMA(DIF, 9)
MACD  = 2 × (DIF − DEA)          # 国内看盘软件的 MACD 柱
```

EMA 用 `series.ewm(span=n, adjust=False).mean()`。

落盘列名：`MACD_12_26_9`(=DIF) / `MACDs_12_26_9`(=DEA) / `MACDh_12_26_9`(=柱)。

### 2.5 均线族

`ma_periods=[5, 10, 20, 60, 120, 250]`，`ema_periods` 同周期。
**MA250 是最长回看** —— 这就是模拟盘滚动窗口取 300 根的原因
（实测 250 根时末行 NaN 才归零）。

另产出：KDJ(9,3,3)、OBV、量能均线(vol_ma5/10/20)。
`analyze_all` 共产出 **33 列**。

## 3. 信号规则（触发条件）

`generate_all()` 按固定顺序调 4 条规则，结果汇总：

| 顺序 | 规则 | 买入 | 卖出 | 强度 |
|---|---|---|---|---|
| 1 | **MA 交叉** | `prev_ma5 ≤ prev_ma20` 且 `curr_ma5 > curr_ma20`（金叉） | 反向（死叉） | medium |
| 2 | **MACD** | `prev_DIF ≤ prev_DEA` 且 `curr_DIF > curr_DEA` | 反向 | medium |
| 3 | **RSI** | `< 30` 超卖；`< 20` 升级 strong | `> 70` 超买；`> 80` 升级 strong | medium / strong |
| 4 | **布林带** | `close ≤ 下轨` | `close ≥ 上轨` | **strong** |

强度权重：`weak=1`、`medium=2`、`strong=3`。

### 3.1 两个关键实现细节

1. **每条规则只看 `df.iloc[-1]`（最新一根）**
   MA/MACD 例外——它们需比较 `iloc[-2]` 与 `iloc[-1]` 判断"是否刚交叉"，
   即**只有交叉发生的那一天**才发信号，不会连续发。
   RSI/布林则是"当前状态"，只要持续满足就每天发。

2. **不对称性**：布林带一命中就是 strong(3)，单独即可达到买入阈值；
   RSI 多数情况是 medium(2)，单独不够。这让布林带成为最强触发器。

## 4. 策略决策（加权打分）

```python
net_score = Σ买入信号强度 − Σ卖出信号强度

net_score >=  3  → BUY   confidence = min(0.95, 0.6 + net_score × 0.05)
net_score <= -3  → SELL  confidence = min(0.95, 0.6 + |net_score| × 0.05)
其余             → HOLD  confidence = 0.5
```

**必须净分 ≥ 3 才买**。推论：
- 单个 medium 信号（2 分）**不够**
- 单个 strong 信号（3 分）**刚好够**
- 两个 medium（4 分）**够**

这就是 000002 那笔需要 RSI(medium=2) + 布林带(strong=3) 同时命中的原因。

## 5. 仓位与目标价

### 5.1 策略侧（`_calculate_position`）

```
base = confidence × 60
趋势加成: up +20 / sideways 0 / down −20 / unknown 0
position_pct = clip(base, 0, 100)
```

### 5.2 引擎侧（`PaperEngine`，真正决定股数）

```python
pct    = max(0.01, min(0.20, position_pct / 100))   # 单笔硬上限 20%
shares = int(cash × pct / ref_price / 100) × 100    # A 股整百
if shares < 100: continue                            # 买不起一手则放弃
```

⚠️ **两层是不同的**：策略算 31%、引擎夹到 20%。这是**风控**，不是 bug。
`max_position_pct=0.20` 是 `PaperEngine` 的构造参数。

### 5.3 目标价 / 止损（仅 BUY 时）

```
target_price = close + 2 × ATR14
stop_loss    = close − 1.5 × ATR14
```

SELL 时只给 `stop_loss = close + 1.5 × ATR14`（做空语境，A 股不适用）。

## 6. 撮合（T+1，防未来函数）

- **信号日收盘后**跑策略 → 生成订单
- **成交日**用**次日开盘价**成交（`Broker.match_by_date` 取 `bar["open"]`）
- 隔夜 `unfreeze_all()` 解冻

费用：佣金 5 元（最低）+ 过户费 `0.0161%` + 印花税（仅卖出）。

> 这一层是 PRD 的 FR-P0-04：**绝不用信号日收盘价成交**，否则就是未来函数。

## 7. 趋势过滤器（TrendFilter）—— 默认关闭

`ManualStrategy(use_trend_filter=False)` 是默认值。开启后会在决策前拦截逆势操作：

```
if action != HOLD and trend_filter.should_block(action, df):
    action = HOLD; confidence = 0.3
    reason = f"趋势过滤拦截{original} (趋势={state}, 原信号: {reason})"
```

**为什么关**：代码注释记录了分组对照实验（`experiments/run_trend_filter_test.py`，
动态池，2019-2024 六组市场环境）：

| 市场 | 原始超额 | 开启过滤后 | 变化 |
|---|---|---|---|
| 上涨市平均 | −42.66% | −45.02% | **−2.36pp（恶化）** |
| 下跌市平均 | +37.48% | +33.37% | **−4.11pp（抗跌受损）** |
| 横盘市平均 | +5.90% | +0.78% | 波动 |

核心目标（改善上涨市）**未达成**，且跌市抗跌能力受损，净效果为负。
模块保留供后续研究，**不要在未重新验证前启用**。

## 8. 真实成交复盘（T000001 / 000002）

用上面规则回看那唯一一笔买入：

| 环节 | 值 | 来源规则 |
|---|---|---|
| RSI | **24.1** → `<30` 命中，未 `<20` → **medium(2)** | §3 RSI |
| 布林带 | close 8.00 ≤ 下轨 8.23 → **strong(3)** | §3 布林带 |
| MA5/MA20 | 未金叉（MA5=8.53 < MA20=9.21，空头） | §3 MA |
| MACD | 未金叉 | §3 MACD |
| **net_score** | 5 − 0 = **5 ≥ 3** → **BUY** | §4 |
| confidence | 0.6 + 5×0.05 = **0.85** | §4 |
| 趋势 | `down`（MA5<MA20<MA60）→ position 31% | §5.1 |
| 引擎夹紧 | → **20%** | §5.2 |
| 股数 | 10000×0.20/8.00 → **200 股** | §5.2 |
| 目标价 | 8.00 + 2×0.281 = **8.56** | §5.3 |
| 止损价 | 8.00 − 1.5×0.281 = **7.58** | §5.3 |
| 成交 | 次日（2024-04-08）开盘 **8.05**，佣金 5 元 | §6 |

落盘记录可核对：`signal_source=BUY`、`signal_confidence=0.85`、
`executed_at=2024-04-08`、`price=8.05`、`shares=200`、`total_fee=5.02`。

## 9. 策略的真实特征（观察期要回答的）

这是**逆势抄底**，不是趋势跟踪：

- 触发条件要求价格**已经跌了一波**
- 策略自己知道在逆势（`reason` 里写 `趋势=down`）
- 拦截逆势的开关实测是负收益，所以关着

**预期表现**：震荡市 / 急跌反弹里表现好；单边阴跌里会连续接飞刀
（000002 后来浮亏 −11.3% 正是后者样本）。

观察期目标（见 HANDOVER 第三节）：**≥20 笔成交、2-4 周**，
期间**不得修改策略**，只记录胜率、盈亏比、最大回撤。

## 10. 已知边界（诚实清单）

| 项 | 状态 |
|---|---|
| 信号只用最新一根 bar | 设计如此（非缺陷），但意味着持续超卖会每天触发 |
| RSI/布林是"状态型"信号，MA/MACD 是"事件型" | 二者混用时权重不对称，布林带实际主导决策 |
| 趋势过滤器关闭 | 实测负收益，待重新验证 |
| 滑点模型 | `slippage_cost` 恒 0（未建模） |
| 涨跌停 | Broker 已实现（PRD FR-P0-05） |
| 单笔 20% 上限 | 引擎硬编码，非配置项暴露到策略 |
