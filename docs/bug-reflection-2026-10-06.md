# Bug 反思记录：2026-10-06 质量审查

> QM-5 Bug 反思循环产出。本文档记录一次"测试全绿但缺陷真实存在"的
> 深度审查过程，**重点不在单个 bug，而在这类 bug 为什么会集体逃逸**。

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

4. **`web/app.py` 覆盖率仅 14%**（795 行），
   且含 `_pipeline_instance` 全局状态、多 worker 不共享等已知问题。
   建议列为下一个技术债优先项。
