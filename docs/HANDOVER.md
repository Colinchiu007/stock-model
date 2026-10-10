# 项目交接文档

> **交接对象**：下一个接手的 Agent
> **交接日期**：2026-10-09（2026-10-10 更新：定时运行已完成 + 质量门禁修复）
> **仓库**：`Colinchiu007/stock-model`（A股量化分析工具）
> **文档性质**：接手前必读。读完能明白「做了什么、为什么这么做、接下来该做什么」。

> **测试基线说明**（本地装齐可选依赖实测；**CI 数字以 CI 为准**）
> - 本地：**760 passed, 1 skipped**（共 761）
> - **`mypy src/stock_model`：Success, 0 errors**
>   （2026-10-10 之前是 86 errors —— 因为 CI 装了 mypy 却从不执行它）
> - 覆盖率：**85%**
> - 本轮新增 20 个测试：质量门禁锁 8 / 组合优化分派与守卫 8 / 其它 4
> - PR #19 的增量：`test_paper_store.py` 那条 skip 改为真断言 + 59 个持久化与调度测试
> - ⚠️ **不要拿一个环境的数字去改另一个环境的文档**：缺 fastapi 时 `test_web_app.py`
>   整体算一个 skip 条目，各 job 的收集总数本就不同
> - 状态：**无未合并 PR**

---

## 一、一句话现状

**模拟盘已能持久化 + 定时自动运行**（2026-10-10 完成），
动态选股 / 前端 5 个 Tab / 评估文档均已落地，
**类型检查门禁已从「装了不跑」修成「真的在跑」**。

**下一步不是写代码，是让它真实跑 2-4 周收集数据**（见第三节、第七节）。

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
| **模拟盘 API** | `web/paper_api.py` | 13 个端点（含 4 个定时运行端点） |
| **前端界面** | `web/static/` | 5 个 Tab，从内嵌字符串拆成独立文件 |
| **纯 pandas 指标** | `analysis/indicators.py` | MACD/RSI/BOLL/ATR/KDJ/OBV 无依赖实现 |
| **状态持久化**（2026-10-10） | `paper/store.py` + `web/paper_api.py` | 账户 + 推进游标原子落盘，重启恢复 |
| **定时运行**（2026-10-10） | `paper/scheduler.py` | APScheduler；交易日判断、失败可见、配置落盘自恢复 |

**A 股规则已实现**：T+1 冻结、100 股整手、佣金万3（5元下限）、
印花税卖出 0.05%、过户费、涨跌停不撮合、单票 ≤20%。

### 2.3 文档沉淀

| 文档 | 内容 |
|------|------|
| `docs/strategy-evaluation-2026-10-07.md` | 策略评估报告（含**负面实验记录**） |
| `docs/bug-reflection-2026-10-06.md` | 15 个缺陷复盘 + 逃逸分析 |
| `docs/phase4_prd_paper_trading.md` | 模拟盘 PRD（含持久化 / 定时运行 / 13 个端点） |
| `experiments/*.py` | 4 个可复现实验脚本 |
| `docs/bug-reflection-2026-10-10.md` | **假门禁（mypy 装了不跑）+ 空组合静默成功**，含 5 步 SOP 与变异验证记录 |
| `experiments/verify_paper_restart.ps1` | **可复现**的「重启不丢状态」端到端验证（真起服务 + 真强杀） |

### 2.4 质量门禁修复：一个假门禁 + 一个静默成功的空组合（2026-10-10）

体检（Phase 4）时用 `mypy` 扫了一遍，撞出两件事，详细 5 步 SOP 见
[`docs/bug-reflection-2026-10-10.md`](bug-reflection-2026-10-10.md)：

| # | 发现 | 引入点 | 性质 |
|---|------|--------|------|
| A | **CI 装了 mypy 却从不执行它** —— 存量 86 个类型错误无人可见 | `8097d59`（引入 CI 的提交） | 假门禁 |
| B | **组合优化在收益率缺失时返回 200 + 空组合**（`weights={}`, `total_value` 悄悄变 0） | `c7feba9`（Phase 2） | 静默错误结果 |

处理结果：

- mypy **86 errors → 0**，并接入 CI 的 Lint job（`mypy src/stock_model`）
- 豁免清单**点名**写进 `[[tool.mypy.overrides]]`（11 个依赖），刻意不用全局
  `ignore_missing_imports` —— 全局忽略会让"新引入无类型依赖"彻底不可见
- 44 个逐个修复的错误里，**真实运行时缺陷 = 0**：全部是注解缺失 / 变量复用致推断错误 /
  联合类型误报。这条结论很重要 —— 别把"类型检查报错"直接当"有 bug"
- 组合优化：优化器改为**显式抛错**，端点按分支补守卫并返回 400
- 新增 8 个质量门禁锁 + 8 个组合优化锁，**4 次变异验证全部确认会红**
  （其中一次变异暴露了"端点守卫没被独立锁住"，已补）

> **为什么值得记进交接文档**：A 是「装了/配了但不跑」，B 是「返回成功但结果是错的」——
> 两者都是本项目 15 个历史缺陷的同款形态。README 里那句 `覆盖率 81%` 也是同一类
> （实际 85%，已修正）。

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
**`ManualStrategy(use_trend_filter=...)` 默认 `False`**，有测试锁住（`tests/test_trend_filter.py`）。

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

## 四、P0 定时运行：**已完成**（2026-10-10）

### 4.1 交付内容

| # | 内容 | 落点 |
|---|------|------|
| ① | **持久化接线** | `web/paper_api.py`：`_get_engine()` 恢复、`step`/`run` 落盘、`reset` 清盘 |
| ② | **定时调度** | `paper/scheduler.py`（APScheduler）+ `/api/paper/schedule` 开启/停止/查询/立即执行 |
| ③ | **端到端验证** | `experiments/verify_paper_restart.ps1`（真实起服务 → 强杀 → 重启 → 比对） |

### 4.2 验证证据（真实数据，可复现）

`pwsh -NoProfile -File experiments/verify_paper_restart.ps1` 实测输出：

```
跑真实数据: steps=200 trades=8 persisted=True
  区间 2024-01-03 ~ 2024-11-04   总资产=10401.64
开启定时:   running=True next_run=10/12/2026 15:30:00
强杀进程:   data/paper/default.json (34384 bytes) + schedule.json
重启后:     总资产=10401.64 成交=8 交易日=200  最新一笔 T000008 @ 2024-09-27
           OK 账户/成交/资金曲线全部一致
           OK 定时任务已自动接回(下轮 10/12 15:30)
继续推进:   2024-11-05 ~ 2024-11-06 (不重放历史, 资金曲线 202 点无重复)
=========== E2E 全部通过 ===========
```

⚠️ 注意 `next_run=10/12`（周一）：周末被 cron 结构性排除，不是靠任务内判断。

### 4.3 接线时踩过的三个坑（都已写进代码注释）

这三个坑的共同特征还是**不报错**：

| # | 坑 | 症状 | 处理 |
|---|---|------|------|
| 1 | **只恢复账户、不恢复推进游标** | 重启后 `step()` 从数据区间开头重推，在交易过的日期上再交易一遍 —— 成交记录**变多**而不是变空，资金曲线出现重复日期 | 游标与账户一起落盘：`PaperEngine.state_dict()` / `load_state()`，游标存在 `_metadata.cursor` |
| 2 | **先建空账户再替换 `engine.account`** | `Broker` 仍绑在被丢弃的空账户上，之后成交全记到"影子账户"，界面上的账户永远不动 | 账户通过**构造参数**注入，让 `Broker` 从一开始就绑对（`PaperEngine(account=...)`） |
| 3 | `CronTrigger` 的 import 在 `_ensure_scheduler()` **之前** | 缺 apscheduler 时抛裸 `ImportError`，调用方的 `except MissingDependencyError` 接不住 → 变 500，且重启恢复会失败 | 先 `_ensure_scheduler()` 再 import trigger（`test_restore_reports_missing_dependency` 锁住） |

第 3 条是**测试抓出来的**，不是看代码看出来的 —— 写"失败可见"的测试是划算的。

### 4.3.1 ⚠️ 验证脚本自己也骗过我一次（本轮最值得记住的教训）

第一版 `verify_paper_restart.ps1` **全绿通过**了两轮，但**什么都没验证到**：

| 环节 | 真实情况 |
|---|---|
| `Start-Process python` | Windows 上 `python` 解析成 `C:\windows\system32\python.cmd` —— 一个 **cmd 外壳**；真正跑 uvicorn 的 `python.exe` 是它的**子进程** |
| `Stop-Process -Id $wrapper` | 只杀掉外壳，真进程继续占着端口 |
| 之后的"重启" | `Wait-Health` 连到了**同一个还没死的服务**上 |
| 结果 | "重启后状态完全一致"必然成立 —— 因为压根没重启 |

**识破它的是数据对不上**：第二次跑 200 个交易日，区间却是 `2024-11-07 ~ 2025-08-29`
（接着上一次的进度），交易日数 `402 = 202 + 200`。干净起点应该永远从 `2024-01-03` 开始。

最终版加了三道结构性防线，任何一道不满足就直接失败：

1. 用**真解释器**（`python -c "import sys;print(sys.executable)"`）启动
2. 启动前**确认端口空闲**（否则会连到旧服务）
3. 跑之前**确认账户是干净的**（`交易日=0 成交=0`）

教训与项目既有结论一致：**"全绿"可能是假保险，要问自己"这个绿灯有没有可能恒亮"。**
真正的重启验证必须能失败 —— 先确认旧服务真的死了，再谈新服务。

另外两个实测记录在案的设计决定：

- **`Account` 没有 `symbols` / `data_source` / `start_date`**（踩过 `AttributeError`）。
  这些存在 `_metadata` 里，`store.load_metadata()` 负责读回来。取数区间必须与落盘时
  一致 —— 游标存的是**数据行下标**，窗口一变下标就指向别的日期。
- **换股票池 = 重置账户**。旧成交/资金曲线是在旧池子上产生的，拼接两段会让夏普、
  回撤、超额全变成无意义的混合体。故 `_rebuild_engine()` 显式重置并在响应里返回
  `account_reset=true`，同时立刻落盘（免得重启后旧账户"复活"）。

### 4.4 定时运行的默认行为

| 项 | 值 |
|---|---|
| 默认时间 | 每交易日 **15:30**（15:00 收盘后 30 分钟），每次推进 1 个交易日 |
| 周末 | cron `day_of_week=mon-fri` 排除 |
| 节假日 | 可选 `data/paper/holidays.json`（`["2026-10-01", ...]`）<br>**未提供时只排除周末**，`GET /api/paper/schedule` 返回 `holiday_calendar=false` + warning（刻意不内置一份可能过期的节假日表） |
| 重入 | `max_instances=1` + `coalesce=True`：上轮没跑完就跳过，休眠错过只补跑一次 |
| 失败 | 成功/失败/跳过全部写入可查询 status（`run_count`/`error_count`/`skipped_count`/`consecutive_failures`/`last_error`），异常进日志 |
| 落盘失败 | 「跑成功但没存下来」按**失败**处理 —— 否则会出现"每天都在跑、账却不动" |
| 重启 | 配置写 `data/paper/schedule.json`，`create_app()` 时自动接回；缺依赖显式报错 |
| 并发 | 定时任务在线程池、API 在事件循环，故每账户一把可重入锁，防止同一笔挂单被撮两次 |

启用方式（单 worker！）：

```bash
curl -X POST localhost:8000/api/paper/schedule -H 'Content-Type: application/json' \
     -d '{"account_id":"default","hour":15,"minute":30,"days":1}'
curl localhost:8000/api/paper/schedule          # 查状态(含最近一次成败原因)
curl -X DELETE localhost:8000/api/paper/schedule
```

### 4.5 🟢 P2：PRD 记录的技术债

| 编号 | 内容 | 优先级 |
|------|------|--------|
| TD-01 | 多 worker 状态共享 | 已收口为「启动即报错」守卫；真正共享需抽独立服务。**定时调度同样假定单 worker**（每进程一份调度器会让同一账户被重复推进） |
| TD-02 | 前端没有定时开关 | 目前只能通过 API 开启，Tab 上无按钮 |
| TD-03 | RL Agent 超参调优（Optuna） | P3 待办 |
| TD-05 | Web Dashboard 用户认证 | P4 待办 |
| TD-06 | 实时行情 WebSocket | P4 待办 |
| TD-07 | Docker 化部署 | P4 待办 |
| TD-08 | **定时失败没有通知渠道** | 出问题只会进日志与 `GET /api/paper/schedule` 的 `last_error`。项目已有 `notify/`（控制台/文件/Webhook 通道），可复用 |

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

3. **CI 的 Lint job 跑三项：`ruff check` / `ruff format --check` / `mypy src/stock_model`**
   - 合并前本地先跑一遍能省一轮往返
   - ⚠️ **mypy 需要依赖在场**：缺依赖时它只会报一堆 "Cannot find implementation"，
     看着跑了其实没查。所以 Lint job 会 `pip install -e ".[dev,quant,schedule,web]"`，
     本地验证也应保持同一套依赖
   - 无 optional 依赖的环境（只装 `.[dev,quant]`）是 CI 主 job 的真实状态，
     **推送前应模拟**（`pip uninstall apscheduler fastapi starlette` 后跑）
   - 不想动环境也可以用 stub 模块挡在 `PYTHONPATH` 前面。⚠️ 但 stub 只挡你列出的包，
     **pass/skip 的拆分与真实 CI 会不一致**：本轮 stub 模拟出 `674 passed / 28 skipped`，
     真实 CI 是 `666 passed / 36 skipped`（收集总数 702 一致）。
     结论：stub 能用来确认「不会失败」，**不能用来写文档里的数字** —— 数字以 CI 为准。

### 5.3 测试方法论（本项目最重要的方法论）

**变异验证**：写完锁，**拆掉修复，确认锁会红**。

```
变异: 移除关键逻辑 → 测试是否失败？
  全绿 = 假保险，锁没锁住目标
```

本项目已 4 次靠变异验证发现问题（均价测试、文档警示测试、
`RetryError` 测试、基准测试）。其中后两条最初都是**假保险** ——
测试 mock 抛的是 `RuntimeError` 而真实故障是 `RetryError`。

2026-10-10 又做了 **6 次**变异验证，每次都确认锁是真的：

| 变异 | 变红的锁 |
|------|---------|
| 去掉 `engine.load_state(metadata)` | `test_restart_does_not_replay_history` |
| 去掉「落盘失败按失败处理」 | `test_persist_failure_counts_as_failure` |
| 抽掉 CI 里执行 mypy 的步骤 | `test_ci_actually_invokes_mypy` + `test_mypy_step_targets_real_source` |
| 优化器 `_assert_has_returns` 改回空组合 | `test_empty_returns_raises_instead_of_empty_portfolio[3]` |
| 只抽掉端点的 returns 守卫 | `test_returns_based_method_without_history_is_rejected[3]` |

> 最后一条是**变异验证逼出来的补锁**：起初端点用例只断言 `400` + `"收益率" in text`，
> 而优化器的报错同样满足这两条 —— 抽掉端点守卫测试照样绿，**等于守卫没上锁**。
> 补上「这句措辞只有端点守卫会产生」之后才真正锁住。
> **教训：一个契约被两处实现满足时，测试锁的是契约，不是任何一处实现。**

**验证脚本同样要做"能不能红"的检查**（见 4.3.1）：
端到端脚本如果永远连到同一个服务上，它会**恒绿**。

**测试不硬编码随环境变化的数字**（如测试总数、收集数），
只做自洽校验。

**注释里的承诺要 grep 验证**：
本项目多处「注释说覆盖 X，实测 grep 命中 0」。
⚠️ 反过来也成立：**注释里提到某个配置键名时，静态检查要先把注释剥掉再匹配** ——
本轮写 `test_no_global_ignore_missing_imports` 时，就因为注释里引用了
`ignore_missing_imports` 这个词而误报。

### 5.4 不可触碰的约束

| 约束 | 原因 |
|------|------|
| **单 worker 运行** | `_assert_single_worker` 会拒绝多 worker（TD-01） |
| **Python ≥3.10** | CI 跑 3.10/3.11/3.12 矩阵 |
| **不上交代码注释里的 TODO** | 注释承诺与实际覆盖脱节是本项目最大的缺陷来源 |
| **门禁必须真的执行** | 「装了/配了但不跑」比没有门禁更危险（mypy 案：从 CI 引入起就没执行过） |

---

## 六、常用命令速查

```bash
# 全部测试（本地装齐可选依赖: 760 passed, 1 skipped）
PYTHONPATH=src pytest tests/ -q

# 模拟 CI 主 job（无 optional 依赖；数字会与本地不同, 别互相抄）
pip uninstall apscheduler fastapi starlette
PYTHONPATH=src pytest tests/ -q

# 覆盖率（当前 85%）
PYTHONPATH=src pytest tests/ -q --cov=stock_model --cov-report=term

# lint + 类型检查（提交前必跑；以 CI 版本为准）
ruff check src/ tests/
ruff format --check src/ tests/
mypy src/stock_model          # 需要依赖在场, 否则只会报找不到库

# 启动服务（单 worker！）
uvicorn --app-dir src "stock_model.web.app:create_app" --factory --port 8000

# 开启模拟盘定时运行（默认每交易日 15:30 推进 1 天）
curl -X POST localhost:8000/api/paper/schedule \
     -H 'Content-Type: application/json' \
     -d '{"account_id":"default","hour":15,"minute":30,"days":1}'
curl localhost:8000/api/paper/schedule            # 查状态(含最近一次成败原因)
curl -X POST localhost:8000/api/paper/schedule/run   # 立即跑一次(与定时同一路径)
curl -X DELETE localhost:8000/api/paper/schedule  # 停止

# 端到端验证「重启后状态还在」(真实网络 + 真起服务 + 真强杀)
pwsh -NoProfile -File experiments/verify_paper_restart.ps1

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

2. 把定时运行打开，让它真实跑 2-4 周   ← 现在唯一该做的事
   ├─ 单 worker 启动，POST /api/paper/schedule（默认每交易日 15:30）
   ├─ 隔几天 GET /api/paper/schedule 看一眼 run_count / error_count
   │    └─ error_count 涨了就是真出问题，别假设"它自己在跑"
   └─ 想要精确排除节假日就补 data/paper/holidays.json（不提供则只排周末）

3. 收集到足够数据（建议 ≥ 20 笔成交）后，再评估策略
   ├─ 那时才有资格谈「实盘信号质量」
   └─ 别在没有新数据时重复本轮的试错（详见第三节）

4. 若要动前端：目前定时开关只有 API，Tab 上没有按钮（TD-02）
   └─ 后端状态含 next_run_time / last_status / warnings，够画一个状态卡
```

> ⚠️ **别把定时运行当成"设好就不用管了"**：目前没有通知渠道，
> 出问题只会进日志和 `GET /api/paper/schedule` 的 `last_error`。
> 想省心就加一条盯着 `consecutive_failures` 的检查（或在 CI 之外的 cron 里 curl 一下）。

---

## 八、一句话提醒

**这个项目最深的坑不是代码，是「看起来正常的结果其实是错的」。**

本轮 15 个缺陷里，多个会产出**漂亮但错误的数据**
（静默输出 0、显示「运行中」但没跑、超额改善 39 个百分点的伪信号）。

2026-10-10 这一轮又添了一个同款：**验证脚本自己连到了没被杀掉的服务上，
于是"重启后状态一致"必然成立 —— 全绿，却什么都没验证到**（见 4.3.1）。

接手后请保持三个习惯：
- **写完锁做变异验证** —— 全绿可能是假保险
- **问自己"这个绿灯有没有可能恒亮"** —— 尤其是端到端验证
- **看到反常结论先查数据来源** —— 别急着下结论

🤖 本文档由 Mavis 生成于 2026-10-09