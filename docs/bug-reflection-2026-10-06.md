# Bug 反思记录：质量审查（2026-10-06 起，持续追加）

> QM-5 Bug 反思循环产出。本文档记录一次「测试全绿但缺陷真实存在」的
> 深度审查过程，**重点不在单个 bug，而在这类 bug 为什么会集体逃逸**。
>
> **本文档持续追加**：首轮记录 5 个缺陷，其后又陆续修出 10 个。
> 完整清单见下方「缺陷总表」。

## 缺陷总表（截至 2026-10-07，共 15 个）

| # | 缺陷 | 类型 | 严重度 | 引入 commit |
|---|------|------|--------|-----------|
| 1 | 流水线风控因 `shares=0` 短路，`BLOCKED` 永不可达 | 逻辑 | 高 | `59d7907` |
| 2 | `aggregate_signal` 丢弃全部价格字段（1 的深层根因） | 逻辑 | 高 | `59d7907` |
| 3 | demo/README 读不存在的 metrics key，静默输出 0 | 一致性 | 中 | `3c9a3c6` |
| 4 | 版本号 `pyproject` 0.1.0 vs `__init__` 0.5.0 | 一致性 | 中 | `3bc855a` |
| 5 | README 引用从未实现的 `momentum` 模块 | 文档 | 低 | `c7feba9` |
| 6 | 架构文档引用不存在的 `notify/templates.py` | 文档 | 低 | `c7feba9` |
| 7 | 缺 parquet 引擎 → 全新克隆必然崩溃 | 依赖 | **高** | `3f7f226` |
| 8 | 技术指标在 3.10/3.11 **静默不产生** → 策略恒 hold | 依赖 | **高** | `c7feba9` |
| 9 | pandas-ta 0.4.x 的 BOLL 列名漂移 → 装了反而失效 | 依赖 | 高 | 外部库 |
| 10 | RSI 列名两条代码路径不一致（`rsi14` vs `RSI_14`） | 一致性 | 中 | `c7feba9` |
| 11 | 定时启动三层静默降级 → 界面显示运行中但没跑 | 逻辑 | 高 | `49706f1` |
| 12 | 多 worker 下状态不一致（TD-01） | 架构 | 中 | `49706f1` |
| 13 | `RLTradingAgent.load()` 失败返回 `None`，成败无法区分 | 逻辑 | 低 | — |
| 14 | 「风控拦截」测试断言三种状态都算通过（空断言） | 测试 | 中 | — |
| 15 | 我的**新增测试**自身缺陷导致 CI 失败 3 次 | 测试 | — | 本次审查 |

## 贯穿全部缺陷的一个共同病根

> **失败被静默吞掉，上层却以为成功。**

- 1/2：`shares=0` 短路 → 风控返回空 → 以为在管控风险
- 7：`ImportError` 穿透 tenacity 重试 → 用户只看到 `RetryError`
- 8：指标列不产生 → 信号恒 0 → 以为"市场没机会"
- 11：apscheduler 缺失 → 只 log warning → 界面显示"运行中"
- 13：`load()` 返回 `None` → 调用方无法得知模型没加载上

这五处的代码结构惊人地相似：
**一个返回空/无信号的路径 + 一个乐观假设上层的检查点。**

## 逃逸分析（首轮）

问题不在于测试数量，而在于**测试方式**：

| 逃逸链 | 说明 |
|--------|------|
| `importorskip` 消音 | `39c8483` 用它让缓存测试静默 skip，CI 变绿但缺陷永存 |
| `.get(key, 0)` 兜底 | 把「接口不匹配」从崩溃降级为错误数据 |
| 注释承诺但未落地 | `test_trading_pipeline.py` 头注释写「覆盖风控拦截」，实测 grep 命中 **0** |
| 无契约测试 | README 的 import 示例从无可执行性校验 |
| 版本号双写 | `pyproject.toml` 与 `__init__.py` 各一份，bump 只改一处 |
| commit 标题脱节 | `3c9a3c6` 标题称 fix，实际在制造新 bug |
| **环境盲区** | pandas-ta 相关缺陷只在 3.12 暴露；`tomllib` 只在 3.10 暴露 |

---

# 首轮记录（2026-10-06）

## 摘要

| 项 | 数值 |
|----|------|
| 审查范围 | 全仓库 71 文件 / ~8000 行生产代码 |
| 修复前测试 | 396 passed, 10 skipped |
| 发现缺陷 | **5 个**（全部实测复现，非静态猜测） |
| 修复后测试 | 412 passed, 10 skipped（+16 回归保护） |
| 覆盖率变化 | 76% → 77%（`trading_pipeline.py` 76%→82%） |
| lint | ruff check / format 全绿 |

---

## 发现的问题

### Bug 1 · README 引用从未实现的模块

- **位置**：`README.md:33, 120, 123, 195`
- **引入**：`c7feba9`（"Phase 2 complete"，声称实现动量策略）
- **现象**：`from stock_model.strategy.momentum import MomentumStrategy` → `ModuleNotFoundError`
- **影响**：新用户照抄 README 第一段二期示例即崩
- **修复**：删除该引用（未实现的功能不该出现在文档里），同时补上实际存在但漏记的 `trading_env.py`

### Bug 2 · 流水线风控检查是死代码（最严重）

- **位置**：`src/stock_model/pipeline/trading_pipeline.py:339`（原 `shares=0`）
- **引入**：`59d7907`（"add TradingPipeline + integration tests"）
- **链条**：
  1. `_check_risk` 构造 `Position(shares=0)`
  2. `RiskManager.check_position_risk` 首行 `if position.shares <= 0: return alerts` 短路
  3. → 风控检查恒返回空列表
  4. → `critical_alerts` 恒为空
  5. → `PipelineStatus.BLOCKED` 在生产路径**永不可达**
- **实测**：`shares=0` → `[]`；`shares=100` + 现价腰斩 → 1 条 critical 警报
- **附带语义错误**：原代码把 `target_price` 同时当作成本价和现价。
  但策略语义里 `target_price` 是**目标卖出价**（现价 + 2×ATR），不等于现价。
- **修复**：`_check_risk` 接收真实现价，用"拟建仓 1 手"视角评估，
  并把策略的 `stop_loss` 作为阈值传入

### Bug 2b · 策略聚合丢弃价格字段（修复 Bug 2 时发现的深层根因）

- **位置**：`src/stock_model/strategy/engine.py:688`
- **现象**：`StrategyEngine.aggregate_signal` 返回**全新的** `StrategyResult`，
  只复制 `symbol/action/confidence`，`target_price` / `stop_loss` / `position_pct` 全部丢失
- **影响**：即使修好 `shares=0`，流水线拿到的仍是空价格字段，风控依然无法工作
- **实测**：修复 `shares=0` 后 `run_once` 仍返回 EXECUTED 而非 BLOCKED，
  追查发现聚合结果中 `stop_loss` 为 `None`
- **修复**：从产生最佳动作的那个策略继承价格字段

### Bug 3 · demo.py 与 README 读取不存在的 metrics key

- **位置**：`examples/demo.py:117,119`、`README.md:125`
- **引入**：`3c9a3c6` —— commit 标题写着 `fix demo.py API names`，
  **实际却引入了错误的 key**
- **现象**：读 `sharpe_ratio` / `trade_count`，
  而 `BacktestEngine._calculate_metrics` 实际写 `sharpe` / `total_trades`
- **危害等级高于普通 bug**：因为用 `.get(key, 0)` 兜底，
  **不抛异常、静默打印 0**。用户看到"夏普 0.00、交易 0 笔"会误判策略无信号
- **修复**：改用真实 key

### Bug 4 · 版本号三处不一致

- **位置**：`pyproject.toml:7`（0.1.0）vs `src/stock_model/__init__.py:26`（0.5.0）
- **引入**：`3bc855a`（"bump version to 0.5.0" 只改了 `__init__.py`）
- **影响**：发布流程读 `pyproject.toml`，打出去的包永远停在 0.1.0
- **修复**：同步为 0.5.0

### Bug 5 · 架构文档引用不存在的文件

- **位置**：`docs/phase2_architecture.md:93` — `notify/templates.py`
- **发现方式**：回归测试的"文档引用完整性"检查自动揪出（人工通读时漏掉）
- **修复**：删除该行

---

## 逃逸分析：为什么 396 个测试全绿却没抓到

这是本次审查最有价值的部分。

### 逃逸链 1：测试只验证"有返回"，不验证"语义正确"

`test_trading_pipeline.py` 的文件头注释写着"覆盖：风控拦截/异常"，
但全文 grep `risk|BLOCKED` **命中数为 0**。注释承诺的覆盖从未落地，
且没有任何机制检查注释与实际测试是否一致。

### 逃逸链 2：`.get(key, 0)` 静默兜底

demo.py 用 `metrics.get('sharpe_ratio', 0)` 读取指标。
key 拼错时返回默认值 0，**不报错、不告警**。
这类写法把"接口不匹配"从崩溃降级成了静默错误数据。

### 逃逸链 3：无契约测试

README 里的 import 示例没有任何机制验证可执行性。
文档与代码之间缺少机器可校验的契约。

### 逃逸链 4：版本号双写无单一真源

`pyproject.toml` 和 `__init__.py` 各写一份版本号，
bump 时只改一处，CI 无一致性检查。

### 逃逸链 5：commit 标题与实际改动脱节

`3c9a3c6` 标题为 `fix demo.py API names`，
实际内容是**引入**了错误的 key。标题承诺修复，代码却在制造新 bug。

---

## 预防措施（已落地）

| 措施 | 落地位置 | 防御的逃逸链 |
|------|---------|-------------|
| 回归保护测试套件 | `tests/test_bug_regressions.py`（16 个） | 全部 |
| README import 可执行性校验 | `TestBug1ReadmeImportsExist` | 逃逸链 3 |
| metrics key 契约测试 | `TestBug3DemoMetricsKeys` | 逃逸链 2 |
| 文档源码引用完整性 | `TestGeneralIntegrity` | 逃逸链 3 |
| 版本号单一真源校验 | `TestBug4VersionConsistency` | 逃逸链 4 |
| 语法解析全仓扫描 | `TestGeneralIntegrity` | 通用 |
| 覆盖率产物不入库 | `.gitignore` | 工程卫生 |

### 回归测试的设计原则

针对本次逃逸模式，测试刻意采用**"锁死失败原因"而非"锁死实现"**的方式：

- `test_pipeline_position_has_nonzero_shares` — 用 `inspect.getsource`
  直接断言源码不含 `shares=0`。这样即使有人重构成等价但有 bug 的写法，测试仍会报警。
- `test_run_once_blocks_on_real_stop_loss_violation` — 不使用 mock，
  验证真实生产路径，仅 mock 数据获取边界。
- `test_check_risk_returns_alerts_not_empty` — 显式构造"现价跌破止损"场景，
  注释说明为什么这个场景**应该**报警（避免后来者误判为假阳性而删掉）。

---

## 给后续维护者的提醒

1. **`.get(key, default)` 在读取跨模块数据时要慎用**。
   指标、配置、字典取值时，优先用 `[]` 让它崩出来；
   确实需要兜底时，至少加 `logger.warning`。

2. **commit 标题要描述实际改动**。`fix X` 但实际引入 X 的 bug，
   会让 `git log` 变成误导来源。

3. **注释里的"覆盖 XX"是对自己的承诺**。
   如果做不到，要么补测试，要么删掉注释。

4. **`web/app.py` 覆盖率曾仅 14%**（795 行），
   且含 `_pipeline_instance` 全局状态、多 worker 不共享等已知问题。
   其中 TD-01 已在 2026-10-07 收口为「启动即报错」守卫。

---

# 后续轮次（2026-10-07）

## 第二轮：P0 修复 + 新功能

| 缺陷 | 要点 |
|------|------|
| 7 parquet | 全新克隆必然崩。逃逸链：`39c8483` 用 `importorskip` 消音而非修复 |
| 8 指标失效 | pandas-ta 仅 3.12 可装，项目支持 3.10 → 3.10/3.11 上策略恒 hold |
| 9 BOLL 列名 | pandas-ta 0.4.x 列名变 `BBL_20_2.0_2.0`，**装了反而失效** |
| 10 RSI 大小写 | pandas-ta 分支写 `rsi14`，兜底分支写 `RSI_14` |
| 12 多 worker | TD-01，最终收口为守卫而非共享状态 |

## 第三轮：假成功与守卫

| 缺陷 | 要点 |
|------|------|
| 11 定时假成功 | 三层静默降级：collector 只 log → pipeline 谎报「已启动」→ web 无条件写 running |
| 12 多 worker | 不用 Redis 共享状态（会「界面说停了实际没停」），改为启动期拒绝 |

## 第四轮：系统性扫描（本轮）

不是撞见的，是**主动扫出来的**：

| 扫描模式 | 产出 |
|---------|------|
| 全仓 `except ImportError` 分支逐个审查 | 10 处中 9 处为合理降级，1 处（`rl_agent.load`）是缺陷 |
| 注释承诺 vs 实际测试 grep 比对 | 抓出「风控拦截」测试是**空断言** |
| `.get(key, default)` 扫描 | 均为合法配置读取，无缺陷 |

### 缺陷 13 · `RLTradingAgent.load()` 失败返回 `None`

```python
except ImportError:
    logger.warning("stable-baselines3 未安装，无法加载模型")
except FileNotFoundError:
    logger.warning(f"模型文件不存在: {path}")
    # ← 无 return，调用方拿到 None
```

`is_trained` 属性定义了但**全项目无人使用**，等于没有任何机制能发现加载失败。
后果：加载失败的 Agent 静默降级到规则策略，`analyze()` 照常返回正常结果。

**修复**：返回 `bool`，docstring 示例同步改为检查返回值。

### 缺陷 14 · 「风控拦截」测试是空断言

```python
# 不应崩溃
assert result.status in [
    PipelineStatus.EXECUTED,
    PipelineStatus.SKIPPED,
    PipelineStatus.BLOCKED,
]
```

三种状态**都算通过**，等于什么都没断言。而该文件头注释却宣称覆盖「风控拦截」。

**修复**：改为精确断言 `== PipelineStatus.EXECUTED` 且 `risk_alerts == []`。
另加 `TestNoVacuousAssertions` 防止这类写法回归。

---

## 缺陷 15 · 我自己的新增测试导致 CI 失败 3 次

**这是本次审查最值得记录的部分。**

| 轮次 | 我的错误断言 | 根因 | 修法 |
|------|-------------|------|------|
| 1 | `assert claimed == {total}` | 收集数随 `importorskip` 漂移 | 改为环境无关的自洽校验 |
| 2 | `count <= total * 2` 仍失败 | 跨环境比较本身无意义 | 去掉跨环境比对，只校验自洽 |
| 3 | `numbers.pop()` 后再比较 | pop 清空集合，错误信息打出 `set()` | pop 前先取值 |

**共同的病根**：我用「本地绿灯」当成了「正确」。

- 本地 venv 依赖比 CI 全 → `fastapi`/`apscheduler` 差异本地看不见
- 本地 Python 3.11 → `tomllib` 在 3.10 崩溃
- 本地收集 497 vs CI 收集 469 → 硬编码数字必然挂

**修正做法**：推之前**先把自己的 venv 调整到与 CI 一致**再跑。
比如 CI 主 job 只装 `.[dev,quant]`，就先卸载 fastapi/apscheduler 跑一遍。

### 另一个反复出现的信号

CI 报错里出现 `set()` / 明显异常值时，**往往是断言自身的缺陷暴露了问题**，
而不是被测代码的问题。第三轮正是靠这个 `set()` 定位到 `pop()` 的错误。

### 变异验证：锁必须能自证

新增的锁写完后**必须做变异验证** —— 拆掉修复，确认锁会红：

```
变异1: load() 退回返回 None  → 4 个测试立即失败 ✅
变异2: 断言放宽为 in [...]   → TestNoVacuousAssertions 失败 ✅
```

**不能自证的锁等于没锁。** 全绿但拆掉修复仍绿的测试是假保险。

---

## 阶段性结论

15 个缺陷里，**5 个是同一种病**（失败被静默吞掉，上层以为成功），
**3 个是同一种测试缺陷**（我的断言只在本地成立）。

最大的收获不是修了多少 bug，而是两条可复用的方法：

1. **写验证性测试前先问「这个断言在不同环境下成立吗」**
2. **锁写完做变异验证** —— 拆掉修复，确认锁会红
