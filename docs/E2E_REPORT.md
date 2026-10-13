# 前端 E2E 测试与事件循环阻塞修复（2026-10-11）

## 背景

用户要求对前端**全部功能、按钮、流程**做 E2E 测试并修复问题。
前端共 **5 个 Tab / 24 个交互元素 / 15 个后端 API 调用**。

## E2E 覆盖矩阵（`tests/test_frontend_e2e.py`，16 条用例）

| Tab | 用例 | 覆盖的交互 |
|---|---|---|
| 选股榜 | `test_default_scan_flow` | 默认参数「开始扫描」→ `#sc-body` 出现数据行或"无结果" |
| 选股榜 | `test_empty_symbols_validation` | 清空代码点扫描 |
| 模拟盘 | `test_schedule_card_states` | 状态卡可见、状态文本非空 |
| 模拟盘 | `test_refresh_button` | 「刷新」 |
| 模拟盘 | `test_reset_button_has_confirm` | 「重置账户」**有 confirm**（取消后状态不变） |
| 模拟盘 | `test_schedule_start_stop_roundtrip` | 停止→开启→停止 三步状态机（见下） |
| 模拟盘 | `test_paper_reset_with_accept` | 重置（接受 confirm）→ 资产回到初始 |
| 个股分析 | `test_detail_analyze_renders_result` | 「分析」000001 → 结果渲染 |
| 回测 | `test_backtest_run` | 「运行回测」 |
| 回测 | `test_backtest_form_exists` | 表单与按钮 |
| Pipeline | `test_pipeline_start_stop` | 启动 → 状态 → 停止 |
| Pipeline | `test_interval_input_bounds` | 间隔输入 `min=1` 约束 |
| 全局 | `test_no_js_errors_after_full_tour` | 五个 Tab 走一遍**无 JS 错误** |

### 状态机式断言（防竞态的关键）

`test_schedule_start_stop_roundtrip` 对**任意初始状态**鲁棒：
先压到「未开启」（若已是则跳过点击，避免 no-op 掩盖问题），
每步用 `wait_for_function` **轮询真实 UI 变化**（异步完成时机不定，固定 sleep 会闪失败），
结束时**还原现场**（停止，不留副作用——曾因测试把调度停在"未开启"而险些让次日首跑失效）。

## 🔴 抓到的产品级 bug：同步阻塞端点冻结事件循环

**现象（E2E 稳定复现）**：全量跑时（前序用例做过扫描/分析/回测），
点「停止定时」后 **10 秒+无响应**（UI 卡在"处理中…"）；单跑同一操作毫秒级成功。

**根因**：`screener.py` 的三个端点 `screener` / `analyze_detail` / `backtest_detail`
都是 **`async def` 但内部做同步网络 IO**（拉真实行情，数十秒）。
`async def` 端点**直接在事件循环上执行**——同步 IO 会**冻结整个事件循环**，
期间**所有请求**（任何用户的任何操作）全部排队。

**影响**：真实用户 A 在扫描时，用户 B 的任何操作都会卡死。**产品级缺陷。**

**修复**：三个端点改为 **`def`**——FastAPI 把 `def` 端点丢进**线程池**执行，
不阻塞事件循环。源码注释写明原理（`screener.py` 头部）。

**验证**：修复后全量 E2E 三连跑全过（点停止 3 秒内 `● 未开启 | msg='定时已停止'`）。

## 判读纪律与工具

- **观察期日常**：`python scripts/paper_status.py`（§1b）
- **调度状态机测试**的"还原现场"设计：测试结束必须恢复生产状态（已开启）

## 遗留与诚实声明

- **数据源不稳时**（baostock 周末维护）：扫描/分析会显示明确的根因文本
  （PR #37 修复），E2E 里这类用例**依赖数据源可用性**，
  数据源故障时表现为"错误行"而非失败——判读时注意区分
- Docker 已按用户决策移除（PR #33），与本次无关

---

## 追加：2026-10-13 数据源恢复后的复跑与主题复盘

PR #47（`3bb2737`）合入的三个修复（引擎构造锁隔离 / fetcher 双向降级 /
E2E 数据目录隔离），在数据源恢复后（本地实测：baostock login 成功、
akshare 正常取数）按报告待办复跑 E2E：18 passed 0 errors ——
三个修复的端到端效果确认。复跑同时暴露并修复了一个测试设施缺陷
（netstat 解码崩溃，详见 `bug-reflection-2026-10-13.md`），
并补了一次 CI ubuntu 矩阵逃逸的平台守卫。

**合并观察（缺陷主题）**：PR #47 的三个问题与 10-12 空池假成功、
10-10 假门禁同宗 —— 本系统的难点集中在**跨请求/跨进程的状态与资源隔离**
（锁的粒度、降级的方向、数据目录的归属），单功能逻辑一直很稳。
10-13 新增第二条主题线：**平台/环境差异**（GBK 解码、CI 矩阵）——
本地环境正确不等于其他环境正确，环境相关的代码路径要有显式守卫和回归。
