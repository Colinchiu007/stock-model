# 模拟盘运维手册（面向 agent / 自动化执行）

> **给 agent 的读法**：本文件是可直接照做的操作手册。每条命令都标注了
> 「已验证」与验证方式；**只做手册里写的，不要发挥**。
> 命令默认在仓库根目录 `D:\Data\projects\stock-model-new` 执行，Windows + PowerShell 7。
>
> 设计背景与“为什么”见 `docs/HANDOVER.md`（交接文档）；
> 本文件只讲**怎么做**。两者冲突时，以本文件为准并回报冲突。

## 0. 系统组成（先读这个）

「每天自动跑」由四层保障构成，各有分工、互为冗余：

| 层 | 组件 | 触发方式 | 作用 |
|---|---|---|---|
| 1 | 内置调度器（APScheduler） | 服务进程活着时，每交易日 15:30 | 常规执行 |
| 2 | **每日兜底脚本** `scripts/paper_daily_fallback.py` | 外部调度器（harness）每天一次 | 服务没起就拉起并补跑 |
| 3 | 心跳检测 | 查询 status 时 | 距上次活动超 7 天 → 提示「可能没在跑」 |
| 4 | 失败推送 | notify 通道 | 失败时主动通知（需配 webhook） |

**幂等性**：层 1 与层 2 幂等且互不冲突——先跑成的那个写下了 `last_run_at`，
后跑的看到「今天已处理」就什么都不做。**两层都配，才完整。**

## 1. 每日兜底脚本（给 harness 定时任务调用）

### 1.1 定时任务配置

```
命令:     python scripts/paper_daily_fallback.py
工作目录:  仓库根目录 D:\Data\projects\stock-model-new
频率:     每天 1 次
建议时间:  19:00（在内置调度「每交易日 15:30」之后）
超时:     ≥ 20 分钟（首个交易日需拉取真实行情）
端口:     默认 8123（可用 --port 8123 显式指定）
```

### 1.2 脚本行为（全部幂等）

| 场景 | 行为 | 退出码 |
|---|---|---|
| 服务没起 | 后台拉起服务，等健康检查（最长 60s） | — |
| 今天已处理（成功/**失败**/跳过） | 秒退，什么都不做 | 0 |
| 今天还没跑 | `POST /api/paper/schedule/run` 推进 1 个交易日 | 0 |
| 今天非交易日 | 接口返回 `skipped`，视为已处理 | 0 |
| 拉起失败 / 执行 `error` | 打印原因 | **1** |

- 退出码 `0` = 无需处理或处理成功；`1` = 有异常（harness 应记录/通知）。
- 脚本日志：`scripts/paper_daily_fallback.log`（服务 stdout 追加写入）。

### 1.3 实测记录（2026-10-10，真实环境）

- 服务未起 → 自动拉起 → 健康通过 → 补调度配置 → 执行 ✓
- 非交易日（周六）→ `status=skipped`，退出 0 ✓
- 幂等：连跑第二次秒退 0，`run/error/skipped = 0/0/1`，无重复推进 ✓
- `ok` 路径（真实推进）只在工作日走到，撰写当日（周六）未实测到；
  它与 `skipped` 共用同一条 `POST` 分支，真实推进已由重启 E2E 覆盖 —— **见 §2.3**

## 2. 常用操作（全部已验证）

### 2.1 启动服务

```powershell
uvicorn --app-dir src "stock_model.web.app:create_app" --factory --port 8123
# 健康检查: http://127.0.0.1:8123/api/health  → {"status":"ok"}
```

⚠️ **端口红线**：不要用 8000 —— 本机 8000 被另一个常驻应用（everos）长期占用
（实测：`Get-NetTCPConnection -LocalPort 8000` → `everos.exe`，health 返回 404）。
本项目一律用 **8123**（重启 E2E 的既有约定端口）。

### 2.2 查询定时状态（心跳/健康检查的第一入口）

```powershell
curl.exe "http://127.0.0.1:8123/api/paper/schedule?account_id=default"
```

关键字段（人类可读版在界面「模拟盘」Tab 的「定时运行」卡片上）：

| 字段 | 含义 | 什么时候该警惕 |
|---|---|---|
| `running` | 是否已配置定时 | `false` = 没配 |
| `next_run_time` | 下次执行时间 | 已开启却为空 = 异常 |
| `last_status` | `ok` / `error` / `skipped` | `error` 连续出现 |
| `consecutive_failures` | 连续失败次数 | ≥ 2 |
| `last_run_at` | 上次**任何活动**时间 | 距今超 7 天 → 心跳告警 |
| `alert_channel` | 告警通道 | `(未接入告警通道)` = 失败只会进日志 |
| `warnings` | 所有值得注意的点 | **健康配置下应为空** |

### 2.3 端到端验证（改了调度/持久化相关代码之后必须跑）

```powershell
pwsh -NoProfile -File experiments/verify_paper_restart.ps1
```

真起服务 → 真实行情推进 200 交易日 → **强杀进程** → 重启 → 逐字段比对
（预期：`8 笔成交 / 总资产一致 / schedule.json 仍在 / 定时任务自动接回 / 无重复日期`）。
需要真实网络（baostock），约 3~5 分钟，会清理 `data/paper/*.json`
（**保留** `holidays.json`）。2026-10-10 实测：9 步全过。

### 2.4 前端界面验证（改了前端之后必须跑）

```powershell
python experiments/verify_paper_ui.py
```

真实浏览器（Playwright）9 项断言 + 截图（`experiments/_ui_shots/`），
含「非交易日如实显示未执行」「健康配置下卡片无告警框」。
**跑完必须真的看截图** —— 有一个真 bug 是截图抓到、断言没抓到的。
前置：`pip install playwright && playwright install chromium`（仅本地，CI 不跑浏览器）。

### 2.5 重新生成节假日表（跨年后，或收到「已过期」warning 时）

```powershell
python experiments/generate_holidays.py 2026
```

双源（baostock + akshare）交易日历**逐日比对**，不一致**拒绝写盘**。
今日实测两源只覆盖到 `2026-12-31`，**没有 2027** —— 不要手工编 2027 的表
（把真实交易日误标为假日 → 该跑的那天静默不跑，比没有日历更糟）。

### 2.6 质量门禁（提交/合并前）

```powershell
python -m pytest tests/ -q          # 预期 841 passed, 1 skipped（数字会随测试增加而变，以 CI 为准）
python -m mypy src/stock_model     # 预期 Success: no issues found in 57 source files
ruff check src/ tests/             # 预期 All checks passed!
ruff format --check src/ tests/    # 预期 92 files already formatted
```

**mypy / ruff / 测试数字一律以 CI 为准** —— 本地版本与 CI 不同
（ruff 0.15 vs 0.17 实测判定不同），本地全绿 ≠ CI 全绿。

## 3. 失败通知配置（一次性）

编辑 `.env`（仓库根目录，参考 `.env.example`）：

```bash
STOCK_NOTIFY_WEBHOOK_URL=https://oapi.dingtalk.com/robot/send?access_token=xxx
STOCK_NOTIFY_CONSOLE=true
# STOCK_NOTIFY_FILE_PATH=data/paper/alerts.jsonl
# STOCK_NOTIFY_ALERT_EVERY_N_FAILURES=3   # 连续失败时每 N 次再提醒；0=只提醒首次
```

配完重启服务，`GET /api/paper/schedule` 的 `alert_channel` 应从
`(未接入告警通道)` 变成 `WebhookChannel(<主机名>/***)`。
⚠️ URL 含 `access_token` 属凭据：**接口只回显主机名**，`.env` 不要提交。

## 3b. 仪表盘 API 认证（TD-05）

**未配置 = 不启用**（本地/内网使用不受影响）；**把仪表盘暴露到非本机/公网前必须设置**——
仪表盘有 `POST /api/paper/reset`（清空账户）这类危险端点。

```bash
# .env
STOCK_API_TOKEN=change-me-to-a-long-random-string
```

启用后 `/api/*` 全部要求 `Authorization: Bearer <token>`（401 + WWW-Authenticate）。
豁免：`/api/health`（容器探针不能要求凭据）与静态资源/首页。

**前端页面不输入 token**（无登录页）——两种正确用法：
1. 仅在本机/内网使用（不设 token，回到"未启用"）
2. 公网部署：在反向代理（nginx/caddy）层加认证，token 留给程序调用方
   （`curl -H "Authorization: Bearer $TOKEN" ...`）

比较用 `secrets.compare_digest`（时序安全，有 AST 锁）。
测试：`tests/test_web_auth.py`（12 条）。

## 4. 红线（agent 必须遵守）

1. **单 worker**：服务只能起一个进程（`_assert_single_worker` 会拒绝多 worker）。
   兜底脚本走 HTTP 而非直接 import 引擎，就是为了共用同一个进程状态 ——
   **不要**写"直接调引擎"的变体。
2. **不要杀占用 8000 的进程**（`everos`，与本项目无关）。
3. **端口一律 8123**。
4. **`data/paper/holidays.json` 是被提交的文件**，任何清理都**必须保留它**
   （两个 E2E 脚本已内置保留逻辑；自己写清理时要注意）。
5. **行尾必须 LF**：Windows 上 Python `write_text()` / PowerShell `Set-Content`
   会把 `\n` 写成 `\r\n`，造成整文件 diff（已踩三次）。
   写文件用 `newline=""`（Python）；改完跑
   `pytest tests/test_docs_consistency.py -k test_all_tracked` 验证。
6. **git add 按文件名精确添加**，不要 `git add -A`（本项目已两次把临时文件带进提交）。
7. **本地模拟环境（stub/PYTHONPATH）得到的数字不能写进文档** —— 数字以 CI 为准
   （实测 stub 得 674/28，真实 CI 666/36）。
8. **文档里加粗的环境相关数字必须标来源**（「本地实测」/「CI 实测」），
   CI 会校验（`TestDocNumbersDeclareProvenance`）。
9. **策略不要动**：模拟盘要先真实跑满 2–4 周再评估（HANDOVER 第三节）。
10. **变异验证**：新锁必须变异验证（改掉被保护的行为 → 对应测试必须变红）。
    负向断言的锁，变异要往「多触发」方向打（本项目 4 次假锁的教训）。

## 5. 故障排查速查

| 症状 | 先查 | 处置 |
|---|---|---|
| 界面提示「服务可能根本没在跑」 | 服务进程是否活着 | 没活：`pwsh -File scripts/paper_daily_fallback.py` 会拉起并补跑 |
| `last_status=error` 连续出现 | `last_error` 字段 / 服务日志 | 数据源问题居多；失败已推送告警（若配了 webhook） |
| `holiday_calendar=false` | `data/paper/holidays.json` 是否存在 | 跑 §2.5 |
| 「节假日表已过期」 | 表覆盖年份 | 跑 §2.5 |
| `alert_channel=(未接入告警通道)` | `.env` 是否配置 | §3 |
| 端口被占 | `Get-NetTCPConnection -LocalPort 8123` | 确认是不是本项目服务再处理；**8000 的 everos 不要动** |
| 测试/文档数字对不上 | CI 运行结果 | 以 CI 为准，回填时标来源（§4-8） |

## 7. Docker 部署（TD-07）

> ⚠️ **验证状态（如实）**：`docker compose config` 语法/语义校验通过；
> **容器构建与运行未验证** —— 撰写时本机 Docker 引擎未运行。
> 首次使用请先跑一遍下面的验证步骤，把结果记回本节。

```powershell
# 构建并启动(app + fallback 两个容器)
docker compose up -d --build
docker compose ps                      # 两容器应 Up; app 应 (healthy)
curl.exe http://127.0.0.1:8123/api/health   # {"status":"ok"}
docker compose logs -f app             # 观察启动日志

# 验证兜底(手动触发一次, 不等 19:00):
docker compose exec fallback python /app/scripts/paper_daily_fallback.py
# 预期: ✓ 服务已在运行 → 今日已处理/执行结果 → 退出 0

# 验证幂等(再跑一次应秒退):
docker compose exec fallback python /app/scripts/paper_daily_fallback.py
```

设计要点：
- **两个容器**：`app`(uvicorn) + `fallback`(19:00 后执行每日兜底，共用 `./data` 卷)。
  兜底是进程外保障，分离后 app 崩溃重启不影响兜底节拍
- **单 worker 红线继承**：容器内 uvicorn 不加 `--workers`
- **端口**：宿主 `8123` → 容器 `8000`（避开宿主 8000 被 everos 占用）
- **时区**：`TZ=Asia/Shanghai`（A股调度依赖）
- **数据**：`./data` 挂载到 `/app/data`，账户状态/节假日表持久化；
  `docker compose down` 不丢数据
- **死信开关**：`STOCK_PING_URL` 在两个容器都生效（app 内置调度失败时经 notify；
  fallback 每日 ping 报平安）——见 §8
- 中国网络：构建时打开 compose 里的 `PIP_INDEX_URL` 清华镜像注释

## 8. 死信开关（TD-10 完整形态）

前四层保障都跑在**这台机器上**——机器彻底关机/断网时全部失效。
死信开关（healthchecks.io 模式）补上这块：

1. 在 [healthchecks.io](https://healthchecks.io)（或自建）建一个 Check，拿到 ping URL
2. `.env` 里配 `STOCK_PING_URL=https://hc-ping.com/xxxx`（见 `.env.example`）
3. **Check 的周期设 1~2 天**（兜底每天 ping 一次，留宽余量防误报）
4. 兜底脚本每次运行结束都会 ping：成功 ping 正常端点；**失败 ping `/fail`**（立即告警）
5. 之后你若超过周期没收到 ping，**监控服务主动通知你** —— 与本机是否存活无关

实现：`scripts/watchdog_ping.py`（ping 失败绝不影响兜底退出码，但打日志）。
测试：`tests/test_watchdog_ping.py`（6 条）+ 接线测试（4 条）。

## 6. 变更记录

- 2026-10-10：首版。覆盖 §1–§5 全部命令，均在本会话实测（来源见各节标注）。
- 2026-10-10：新增 §7 Docker 部署（**容器构建未验证**，见节内说明）、§8 死信开关（§1 兜底已接入 ping，实测跳过路径）。
- 2026-10-10：§6 预期测试数改为「以 CI 为准」。
- 2026-10-10：新增 §3b 仪表盘认证（默认关，配置即强制；公网部署前必设）。
