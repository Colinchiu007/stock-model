# 项目交接文档

> **交接对象**：下一个接手的 Agent
> **交接日期**：2026-10-09
> **仓库**：`Colinchiu007/stock-model`（A股量化分析工具）
> **当前 main**：`7c0b90d33`（17 个 PR 已合并，0 个未合并）
> **文档性质**：接手前必读。读完能明白「做了什么、为什么这么做、接下来该做什么」。

> **测试基线说明**：
> - 当前 main：`674 tests collected`（664 passed + 10 skipped）
> - 状态：**已全部合并至 main**，无未合并 PR

---

## 一、一句话现状

项目从「测试全绿但多处静默失效」修到「功能可用 + 评估结论可信」，
**模拟盘 / 动态选股 / 前端界面都已落地**，但**定时自动运行尚未接线**。

---

## 二、这轮工作做了什么（按主题分组）

### 2.1 修复了 15 个「静默失效」缺陷

这是本轮最大产出。共同特征：**代码不报错、日志不打、结果看着正常，但实际是错的。**

| # | 缺陷 | 影响 | 严重度 |
|---|------|------|--------|
| 1 | 流水线风控因 `shares=0` 短路 | `BLOCKED` 状态永不可达，风控形同虚设 | 高 |
| 2 | `aggregate_signal` 丢弃全部价格字段 | 上述问题的深层根因 | 高 |
| 3 | 缺 parquet 引擎未声明 | **全新克隆必然崩溃** | 高 |
| 4 | 技术指标在 3.10/3.11 静默缺失 | pandas-ta 需 3.12，策略「眼睛」瞎了 | 高 |
| 5 | pandas-ta 0.4.x BOLL 列名漂移 | 装了反而失效 | 高 |
| 6 | 定时启动三层静默降级 | 界面显示「运行中」但没跑 | 高 |
| 7 | 基准无数据时超额恒为 0 | **产出「改善 39 个百分点」的伪信号** | 高 |
| 8 | 移动加权均价计算错误 | 二次加仓成本失真 | 中 |
| 9 | 集中度约束对首次买入失效 | 90% 超限未被拦截 | 中 |
| 10 | 单调上涨误报「回撤持续 1 天」 | 指标失真 | 中 |
| 11 | demo/README 读不存在的 metrics key | 静默输出 0，比崩了更难发现 | 中 |
| 12 | 版本号 0.1.0 vs 0.5.0 不一致 | 发布出来的包版本号错误 | 中 |
| 13 | RSI 列名两条代码路径不一致 | 环境相关行为分叉 | 中 |
| 14 | `RLTradingAgent.load()` 失败返回 `None` | 成败无法区分 | 低 |
| 15 | README/架构文档引用不存在的模块 | 照抄即 ImportError | 低 |

**最值得记住的是 #7**：它会产出一个**看似合理但完全错误**的结论
（"换股票池让涨市超额改善 39 个百分点"）。崩溃会让人去查，错误结论会被直接采信。

### 2.2 新增功能

| 功能 | 模块 | 说明 |
|------|------|------|
| **模拟盘** | `paper/models.py` `broker.py` `metrics.py` `engine.py` | 1 万元虚拟资金，**T+1 次日开盘价撮合** |
| **动态股票池** | `paper/universe.py` | 全市场 7423 → 三层筛选 → Top N |
| **选股/分析/回测 API** | `web/screener.py` | 3 个端点 |
| **模拟盘 API** | `web/paper_api.py` | 9 个端点 |
| **前端界面** | `web/static/` | 5 个 Tab，从内嵌字符串拆成独立文件 |
| **纯 pandas 指标** | `analysis/indicators.py` | MACD/RSI/BOLL/ATR/KDJ/OBV 无依赖实现 |

**A 股规则已实现**：T+1 冻结、100 股整手、佣金万3（5元下限）、
印花税卖出 0.05%、过户费、涨跌停不撮合、单票 ≤20%。

### 2.3 文档沉淀

| 文档 | 内容 |
|------|------|
| `docs/strategy-evaluation-2026-10-07.md` | 策略评估报告（含**负面实验记录**） |
| `docs/bug-reflection-2026-10-06.md` | 15 个缺陷复盘 + 逃逸分析 |
| `docs/phase4_prd_paper_trading.md` | 模拟盘 PRD |
| `experiments/*.py` | 4 个可复现实验脚本 |

---

## 三、最重要的结论：**别急着改策略**

### 3.1 已知的事实

用真实数据（1 万元模拟盘，动态分散池，2019-2024 六组市场环境）实测：

| 场景 | 成交 | 策略 | 基准 | 超额 |
|---|---:|---:|---:|---:|
| **上涨市 2019** | 23 笔 | **-1.26%** | +40.07% | **-41.32%** ❌ |
| 下跌市 2021 | 22 笔 | +8.73% | -24.79% | +33.52% |
| 下跌市 2024 | 23 笔 | +14.76% | -28.47% | +43.24% |

**`ManualStrategy` 是震荡市策略，在单边市里会被反复反噬。**

### 3.2 已验证无效的两条路（**别再走一遍**）

| 尝试 | 结果 | 原因 |
|------|------|------|
| 放宽信号阈值 ±3→±1 | 超额几乎不变 | 「多信号共振才动手」是构造特性，非参数问题 |
| 趋势过滤器（拦逆势买卖） | **净负 -1.28pp** | 涨市 -2.36pp、跌市 -4.11pp，核心目标未达成 |

趋势过滤器代码仍在 `strategy/trend_filter.py`，但
**`ManualStrategy(use_trend_filter=...)` 默认 `False`**，有测试锁住。
有专门的 skip 测试提醒这项工作尚未接线完成。

### 3.3 关键教训

**看到统计现象，先问「这是根因还是相关」，别急着动手。**

上一轮我基于「67% 的卖出发生在上涨趋势内」实现了过滤器，
**被数据完全否定**——拦住它并未让涨市变好，还削弱了跌市保护
（A 股「急跌→反弹」形态中，禁止买入反而踏空反弹段）。

### 3.4 建议的下一步

**优先级 1：先跑真实数据，不要先改策略**

让模拟盘按当前配置跑 2-4 周，观察实盘信号质量。
比在历史数据上继续试错有价值得多。

**优先级 2（可选）：降低交易频率**

提高信号确认门槛，减少被反复打脸的次数。
这是验证「问题出在信号质量」这个假设的直接手段。

**优先级 3：考虑换策略方向**

若目标是趋势跟随，需换动量/突破类策略，
而不是在震荡策略上加补丁。

---

## 四、未完成的工作

### 4.1 🔴 P0：定时运行完全没做（用户当前诉求）

**现状**：
- ✅ `paper/store.py` 账户持久化模块已实现（**但 PR #18 未合并**）
- ❌ `web/paper_api.py` **未接线**（`grep paper.store` 结果为 0）
- ❌ 模拟盘**无任何定时调度**

**为什么必须先接线持久化**：

`paper_api.py` 的引擎只存在进程内存的 `_engines` 字典：

```python
_engines: dict[str, Any] = {}   # 进程重启 = 持仓/成交/资金曲线全丢
```

定时跑的场景下，机器重启/发版/崩溃一次就可能丢账户状态甚至重复交易。

**已有资产**：
- `paper/store.py` — `save_account()` 原子写入、`load_account()` 容错恢复
- `paper/engine.py:287` 的 `step()` 可被调度调用
- `data/collector.py:174` 有现成的 APScheduler 用法可参考

**接线时的坑（我踩过）**：

⚠️ `Account` 对象**没有** `symbols` / `data_source` / `start_date` 属性，
这些在 `PaperEngine` 上。我第一次写恢复逻辑时误用了 `restored.symbols`，
直接 AttributeError。股票池等信息存在 `_metadata` 字段里，
需从 `store_path(account_id)` 读 JSON 的 `_metadata` 获取。

### 4.2 ✅ 已完成：PR #18 已合并

```
PR #18  feat/paper-persistence  → main   已合并（7c0b90d）
```

`paper/store.py` 现已在 main 上。**剩下的只有接线与调度。**

### 4.3 🟢 P2：PRD 记录的技术债

| 编号 | 内容 | 优先级 |
|------|------|--------|
| TD-01 | 多 worker 状态共享 | 已收口为「启动即报错」守卫；真正共享需抽独立服务 |
| TD-03 | RL Agent 超参调优（Optuna） | P3 待办 |
| TD-05 | Web Dashboard 用户认证 | P4 待办 |
| TD-06 | 实时行情 WebSocket | P4 待办 |
| TD-07 | Docker 化部署 | P4 待办 |

---

## 五、接手必读：工作方式约定

这些是本轮踩坑总结出来的，**照做能省大量返工**。

### 5.1 环境（Windows）

```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

⚠️ **PowerShell 5.1 不支持 `&&`**，用 `;` 或分行。
⚠️ 系统 pip 受 PEP 668 限制，必须装在 venv 里。

### 5.2 提交与 CI

1. **中文 commit message 用文件传**：`git commit -F msg.txt`
   然后 `grep -c $'\ufffd' msg.txt` 确认无乱码
   （用 write/heredoc 都可能把中文标点压成替换字符，**必须显式检查**）

2. **本地 ruff 版本 ≠ CI 版本**
   - 本地 0.15 vs CI 0.16，判定结果不同
   - 本地全绿不代表 CI 会绿（已栽 2 次）
   - 以 CI 为准

3. **CI 跑 `ruff check src/ tests/` 和 `pytest tests/`**
   - 合并前本地先跑一遍能省一轮往返
   - 无 optional 依赖的环境（只装 `.[dev,quant]`）是 CI 主 job 的真实状态，
     **推送前应模拟**（`pip uninstall apscheduler fastapi starlette` 后跑）

### 5.3 测试方法论（本项目最重要的方法论）

**变异验证**：写完锁，**拆掉修复，确认锁会红**。

```
变异: 移除关键逻辑 → 测试是否失败？
  全绿 = 假保险，锁没锁住目标
```

本项目已 4 次靠变异验证发现问题（均价测试、文档警示测试、
`RetryError` 测试、基准测试）。其中后两条最初都是**假保险** ——
测试 mock 抛的是 `RuntimeError` 而真实故障是 `RetryError`。

**测试不硬编码随环境变化的数字**（如测试总数、收集数），
只做自洽校验。

**注释里的承诺要 grep 验证**：
本项目多处「注释说覆盖 X，实测 grep 命中 0」。

### 5.4 不可触碰的约束

| 约束 | 原因 |
|------|------|
| **单 worker 运行** | `_assert_single_worker` 会拒绝多 worker（TD-01） |
| **Python ≥3.10** | CI 跑 3.10/3.11/3.12 矩阵 |
| **不上交代码注释里的 TODO** | 注释承诺与实际覆盖脱节是本项目最大的缺陷来源 |

---

## 六、常用命令速查

```bash
# 全部测试（main 基线 650 passed；合并 PR#18 后 664 passed）
PYTHONPATH=src pytest tests/ -q

# 模拟 CI 主 job（无 optional 依赖）
pip uninstall apscheduler fastapi starlette
PYTHONPATH=src pytest tests/ -q

# 覆盖率
PYTHONPATH=src pytest tests/ -q --cov=stock_model --cov-report=term

# lint（提交前必跑）
ruff check src/ tests/
ruff format --check src/ tests/

# 启动服务（单 worker！）
uvicorn --app-dir src "stock_model.web.app:create_app" --factory --port 8000

# 四个实验脚本（需真实网络，稳定环境跑）
python experiments/run_regime_test.py            # 分组对照
python experiments/run_threshold_sweep.py        # 阈值敏感性
python experiments/run_dynamic_pool_test.py      # 动态池 vs 伪分散池
python experiments/run_trend_filter_test.py      # 趋势过滤器效果
```

---

## 七、给接手者的建议路径

```
1. 读 docs/strategy-evaluation-2026-10-07.md
   └─ 理解「为什么不要急着改策略」

2. 合并 PR #18（持久化模块）

3. 接线 paper_api + 加定时调度   ← 用户当前诉求
   ├─ ⚠️ 注意 Account 无 symbols 属性，配置在 _metadata（详见 docs/HANDOVER.md 4.1）
   ├─ 必须做「重启后状态还在」的端到端验证
   └─ 完成后去掉 tests/test_paper_store.py 里那条 skip

4. 让模拟盘真实跑 2-4 周，收集实盘数据

5. 基于实盘数据再决定是否调整策略方向
   └─ 别在没有新数据时重复本轮的试错
```

---

## 八、一句话提醒

**这个项目最深的坑不是代码，是「看起来正常的结果其实是错的」。**

本轮 15 个缺陷里，多个会产出**漂亮但错误的数据**
（静默输出 0、显示「运行中」但没跑、超额改善 39 个百分点的伪信号）。

接手后请保持两个习惯：
- **写完锁做变异验证** —— 全绿可能是假保险
- **看到反常结论先查数据来源** —— 别急着下结论

🤖 本文档由 Mavis 生成于 2026-10-09