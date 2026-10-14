# 缺陷反思 2026-10-13：E2E teardown 的 netstat 解码崩溃（数据源恢复后复跑实测）

> 按质量节拍 **Bug 反思循环 5 步 SOP（QM-5）** 执行：根因溯源 → 逃逸链 → 系统性漏洞 → 修复+回归保护 → 预防措施。
> 本轮是「不报错，但结果是错的」家族的又一变种：**异常被吞进线程，结果静默变质**。
> 而激活它的，正是同日一次修复的成功。

## 摘要

| # | 发现 | 性质 | 严重度 | 暴露点 |
|---|------|------|--------|--------|
| A | `text=True` 默认 UTF-8 解码 GBK 的 netstat 输出 → reader 线程炸 → `stdout` 恒 None → teardown 清理崩溃、服务残留 | 代码/环境缺陷 | 中 | E2E 复跑 teardown（本地实测） |
| B | 回归测试无条件运行，CI ubuntu 矩阵上失败（Windows 专用路径无平台守卫） | 流程/门禁缺陷 | 中 | 首推 CI 三个 Test job（CI 实测） |

两个发现串成一条链：A 的回归测试本身又踩了 B —— **本地全量只覆盖 Windows，
不等于覆盖 CI 矩阵**。

---

# 发现 A：netstat 解析的解码崩溃

## 现象

数据源恢复后按 PR #47 报告的待办重跑 E2E：用例全过（本地实测 18 passed 0 errors），
但 teardown 报 `AttributeError: 'NoneType' object has no attribute 'splitlines'`，
且 8128 端口残留一个 E2E 服务进程（pid 34076）没被杀掉。

## ① 第一性原因溯源

`subprocess.run(["netstat", "-ano"], text=True)` 默认按 UTF-8 解码；
中文 Windows 的 netstat 表头是 GBK 字节（含 0xbb 等中文前缀字节）→
subprocess 的 reader 线程抛 `UnicodeDecodeError`。**异常发生在线程里**：
主流程不受任何影响，`CompletedProcess.stdout` 永远停在 None。
调用方 `r.stdout.splitlines()` 才崩 —— 报错点离病灶隔着一个线程，
日志里只留一条 `PytestUnhandledThreadExceptionWarning`。

实测复现（本地）：`text=True` 跑 netstat，`r.stdout is None` → True。
数据行（端口/数字/`LISTENING`）都是 ASCII，只有表头是中文 ——
所以 `errors="replace"` 后解析完全不受影响。

## ② 为什么测试逃了这么久

netstat 分支此前**从未执行过**：`kill_port` 先探测端口，没人听就提前返回；
只有 teardown 时**服务还活着**才会真的调 netstat 解析。而服务这次能活到
teardown，正是因为本轮验证的**复活链修复生效**（服务被兜底脚本拉起并活着）
—— 隐藏路径被「修复的成功」首次激活。teardown/清理路径一向是项目里
覆盖最少的代码，这个组合命中概率最高。

## ③ 修复与回归保护

- 抽出 `_listener_pids(port)` 纯解析函数，`encoding="utf-8", errors="replace"`
  （与项目既有 subprocess 调用风格一致）
- 回归 `tests/test_netstat_listener.py` 2 条（本地实测通过）：起真实子进程
  监听验证解析；刻意**只验证解析、不 taskkill pytest 自身**
- 变异验证：把编码参数退回旧形态，`test_finds_child_listener_pid` 精确变红
- 真实修复确认：修复后的 `kill_port(8128)` 成功杀掉残留服务 34076（本地实测）

## ④ 沉淀

OPERATIONS.md 红线 #11：**subprocess 文本模式必须显式 `encoding=` + `errors=`**。

---

# 发现 B：回归测试的 CI 矩阵逃逸

## ⑤ 根因与修复

A 的回归测试无条件运行，但它保护的是 **Windows 专用路径**
（netstat/taskkill，且被保护的 GBK bug 只在中文 Windows 出现）。
CI 跑 ubuntu，netstat 输出格式不同（`LISTENING` 匹配不到），断言失败 ——
首推三个 Test job 全挂（CI 实测）。修复：`sys.platform != "win32"` 守卫，
二推 6/6 绿（CI 实测）。

## 教训

1. **线程里的异常是「半静默」失败**：主流程继续，结果静默变质（stdout=None）。
   看到 `PytestUnhandledThreadExceptionWarning` 必须当失败查，不能当噪音。
2. **修复的成功会激活新的隐藏路径**：改动让流程走到前所未见的分支时，
   那条分支大概率覆盖最少 —— teardown/清理/异常分支优先补眼。
3. **本地绿 ≠ CI 绿**：平台相关测试必须显式声明适用平台；本地只覆盖 Windows 时，
   推送前要主动设想 ubuntu 矩阵的行为。
4. **报错点 ≠ 病灶点**：跨线程/跨层的崩溃，根因要溯源到「谁产出了这个 None」。

---

# 连带发现 C：CI 门禁盲区 —— scripts/ 从不在 ruff/mypy 覆盖范围

## 现象

按质量节拍对 scripts/ 跑 lint 时发现：**CI 的三个门禁（ruff / format / mypy）
路径参数都停在 `src/ tests/`**，scripts/ 里的违规从未有人见过 ——
一批 ruff 违规 + 9 个 mypy 错误，在盲区里安静躺着。
这与 10-10 复盘的「假门禁：mypy 配了装了从不执行」是同一类缺陷的变种：
那次是「门禁没跑」，这次是「门禁跑了但范围有盲区」。

## 修了什么

1. **scripts/ 4 个文件清理到 ruff/mypy 全绿**（行为中性：导入重排、
   print 折行、删无用的 coding 声明；每个 diff 都人工核对过）
2. **mypy 扩范围后暴露 2 个潜在真 bug，已修**：
   - `paper_daily_fallback.py`：`start_server()` 返回 None 时
     `proc.poll()` / `.returncode` 会 AttributeError 崩掉兜底脚本
     → 加显式守卫返回退出码 1（类型驱动的防御，当前该路径不可达）
   - `watchdog_ping.py`：`ping_quiet()` 声明 `-> bool` 但文档行为是
     「未配置返回 None」→ 标注改为 `bool | None`（诚实于文档）
3. **自动修复的反面教训**：`ruff --fix` 的 RUF100 把 4 处
   `# noqa: S310 / S603`（「固定本机地址」「固定参数」等**安全决策说明**）
   当成无用指令删掉了 —— S 规则确实不在 select 里，但注释承载的决策知识
   不能丢，已恢复为普通注释。**自动修复必须逐条审 diff。**
4. **ci.yml 三个门禁扩展到 scripts/**（改前已排查：无 workflow 契约测试）

## 教训

1. **门禁的范围和门禁本身一样重要**——「跑了」不等于「覆盖了」；
   新增目录时第一件事是问「它在所有门禁的路径参数里吗」
2. 扩门禁的顺序是「先修到干净，再扩范围」——顺序反了就是当场推红
3. 自动 fix 工具只做粗活，**语义性注释（安全决策/为什么）必须人工保住**

## 后续（2026-10-14）：盲区收尾

tests/ 曾以「mypy 只查 src/stock_model」为由被留在门外，攒了 7 个
no-any-return / cast 类历史错误。已全部修掉并把 mypy 覆盖扩展到
`tests/` —— 至此 src/ scripts/ tests/ 三个代码目录全部在门禁内。
