# 模拟盘「重启后状态还在」端到端验证
#
# 为什么需要这个脚本
# ------------------
# 定时运行的**前提**是重启不丢状态。这件事没法用单元测试证明到底 ——
# 单元测试里"重启"只是清空一个字典。这里真的起 uvicorn、真的跑真实行情、
# 真的 kill 掉进程、再真的重启, 然后逐字段比对。
#
# 它验证 5 件事:
#   1. 真实数据能推进并落盘(成交记录非空)
#   2. **强杀进程**(非优雅退出)后, 磁盘上有状态文件
#   3. 重启后 总资产 / 成交 / 资金曲线 / 交易日数 完全一致
#   4. 重启后定时任务自动接回(否则"每天自动跑"会静默失效)
#   5. 重启后继续推进是**往前走**, 不重放历史, 资金曲线无重复日期
#
# 用法(Windows / PowerShell 7):
#   pwsh -NoProfile -File experiments/verify_paper_restart.ps1
#
# 注意:
#   - 需要真实网络(baostock 取数), 故不进 CI
#   - 会写入并清理 data/paper/, 不影响已落盘的其它账户? 会 —— 脚本开头会清空
#     data/paper/*.json, 请在闲置环境跑
#   - 输出同时写 experiments/_verify_output.txt(transcript)
#
# ⚠️ 这个脚本自己也踩过一个坑(值得记住)
# ----------------------------------------
# `python` 在 Windows 上常解析成 `C:\windows\system32\python.cmd` —— 一个 **cmd 外壳**。
# `Start-Process python` 起的是这个外壳, 真正跑 uvicorn 的 python.exe 是它的**子进程**。
# 于是 `Stop-Process -Id $wrapper` 只杀掉外壳, 真进程继续占着端口; 之后 "重启"
# 其实连到了**同一个还没死的服务**上 —— 于是"重启后状态一致"必然成立,
# 但**什么都没验证到**(本脚本第一版就是这么骗过自己的)。
#
# 因此这里做三件事: 用真 python.exe、杀进程树、以及**启动前先确认端口是空的**。

$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $PSScriptRoot
$port = 8123
$base = "http://127.0.0.1:$port"
$paperDir = Join-Path $repo "data\paper"
$keepHolidays = "holidays.json"   # 被提交的节假日表, 不是运行产物, 清理时保留
$transcript = Join-Path $PSScriptRoot "_verify_output.txt"

if (-not (Test-Path (Join-Path $repo "src\stock_model\web\app.py"))) {
    throw "仓库根目录判断错误: $repo"
}

# 真解释器: python 可能是 .cmd 外壳, 直接启动它会让子进程逃过 Stop-Process
$pythonExe = (& python -c "import sys; print(sys.executable)").Trim()
if (-not (Test-Path $pythonExe)) { throw "找不到真实 python 解释器: $pythonExe" }

Remove-Item $transcript -ErrorAction SilentlyContinue
Start-Transcript -Path $transcript -Force | Out-Null

function Test-ServerListening {
    $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    return [bool]$conn
}

function Assert-PortFree($label) {
    if (Test-ServerListening) {
        $owner = (Get-NetTCPConnection -LocalPort $port -State Listen |
                  Select-Object -First 1).OwningProcess
        throw "[$label] 端口 $port 已被进程 $owner 占用 —— 这会让验证连到旧服务上, 结果不可信"
    }
}

function Assert-ServerDown($label) {
    if (Test-ServerListening) {
        throw "[$label] 旧服务仍在监听 $port —— '重启'是假的, 请先杀掉它"
    }
}

function Wait-Health($proc, $label) {
    for ($i = 0; $i -lt 90; $i++) {
        Start-Sleep -Milliseconds 1000
        if ($proc.HasExited) { throw "[$label] 服务进程已退出 (exit=$($proc.ExitCode))" }
        try {
            $r = Invoke-RestMethod -Uri "$base/api/health" -TimeoutSec 3
            if ($r.status -eq "ok") { return $true }
        } catch { }
    }
    throw "[$label] 90 秒内没起来"
}

function Start-Server($logName) {
    Assert-PortFree $logName
    $log = Join-Path $env:TEMP $logName
    Remove-Item $log, "$log.err" -ErrorAction SilentlyContinue
    $p = Start-Process -FilePath $pythonExe `
        -ArgumentList "-m", "uvicorn", "--app-dir", "src", "stock_model.web.app:create_app",
                      "--factory", "--port", "$port" `
        -WorkingDirectory $repo -RedirectStandardOutput $log -RedirectStandardError "$log.err" -PassThru
    Write-Host "  已启动 $pythonExe (pid=$($p.Id))"
    Wait-Health $p $logName | Out-Null
    return $p
}

function Stop-Server($proc) {
    # 没起过 / 已经退出的句柄: 什么都不做。
    # ⚠️ 这里不能去校验端口 —— 端口可能被**另一个**仍在运行的服务占着,
    # 本脚本第一版就是在清理阶段为此抛错, 结果 finally 提前中断,
    # 第二个服务反而没被杀掉(只校验自己杀过的那个)。
    if (-not $proc) { return }
    if ($proc.HasExited) { return }

    # /T 连子进程一起杀, 避免留下占着端口的孤儿
    & taskkill /PID $proc.Id /T /F 2>&1 | Out-Null

    # 等端口真正释放(最多 10 秒), 释放不掉就直接失败 —— 宁可红也不能假绿
    for ($i = 0; $i -lt 20; $i++) {
        if (-not (Test-ServerListening)) { return }
        Start-Sleep -Milliseconds 500
    }
    Assert-ServerDown "Stop-Server"
}

# 兜底: 按命令行精确匹配本脚本起的 uvicorn, 杀掉漏网的孤儿
function Remove-LeakedServers {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*stock_model.web.app:create_app*--port $port*" } |
        ForEach-Object {
            Write-Host "  清理漏网服务 pid=$($_.ProcessId)"
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
    Start-Sleep -Milliseconds 1000
}

function Post-Json($url, $obj) {
    $json = $obj | ConvertTo-Json -Compress
    # 取数是网络往返, 给足超时(200 个交易日约 40 秒)
    return Invoke-RestMethod -Uri $url -Method Post -ContentType "application/json" `
        -Body $json -TimeoutSec 900
}

$s1 = $null; $s2 = $null
try {
    Write-Host "=== 0. 清理旧状态 ==="
    New-Item -ItemType Directory -Force -Path $paperDir | Out-Null
    # ⚠️ 保留 holidays.json —— 它是**被提交**的节假日表, 不是运行产物。
    # 删掉它会让 holiday_calendar 变 false(节假日照常触发), 而脚本自己不会报错。
    Get-ChildItem $paperDir -Filter *.json -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -ne $keepHolidays } | Remove-Item -Force
    Write-Host "(data/paper 已清空, 保留 $keepHolidays)"

    Write-Host "`n=== 1. 第一次启动服务 ==="
    $s1 = Start-Server "paper_e2e_p1.log"
    Write-Host "服务已就绪 (pid=$($s1.Id))"

    Write-Host "`n=== 2. 跑真实数据 (3 只标的 / 200 个交易日) ==="
    # 起点必须是干净账户 —— 否则"重启后一致"可能只是因为压根没重启过
    $fresh = Invoke-RestMethod "$base/api/paper/account?account_id=default"
    $freshTrades = Invoke-RestMethod "$base/api/paper/trades?account_id=default"
    if ($fresh.trading_days -ne 0 -or $freshTrades.total -ne 0) {
        throw "起点不是干净账户(交易日=$($fresh.trading_days) 成交=$($freshTrades.total)) —— 验证不可信"
    }
    Write-Host "  起点已确认干净: 交易日=0 成交=0"

    $run = Post-Json "$base/api/paper/run" @{ account_id = "default"; days = 200 }
    Write-Host "  status=$($run.status) steps=$($run.steps) trades=$($run.trades) persisted=$($run.persisted)"
    Write-Host "  区间: $($run.from) ~ $($run.to)   总资产=$($run.total_asset)"
    if ($run.persisted -ne $true) { throw "落盘失败: $($run.persist_error)" }

    $before = Invoke-RestMethod "$base/api/paper/account?account_id=default"
    $tradesBefore = Invoke-RestMethod "$base/api/paper/trades?account_id=default&limit=5"
    Write-Host "  重启前: 总资产=$($before.total_asset) 成交=$($tradesBefore.total) 交易日=$($before.trading_days)"
    if ($tradesBefore.total -lt 1) { throw "没有产生任何成交, 无法验证'成交记录是否还在'" }
    $firstTradeId = $tradesBefore.trades[0].trade_id
    Write-Host "  最新一笔: $firstTradeId @ $($tradesBefore.trades[0].executed_at)"

    Write-Host "`n=== 3. 开启定时任务(验证重启后能自动接回) ==="
    $sched = Post-Json "$base/api/paper/schedule" @{ account_id = "default"; hour = 15; minute = 30; days = 1 }
    Write-Host "  running=$($sched.running) next_run=$($sched.next_run_time) schedule=$($sched.schedule)"

    Write-Host "`n=== 4. 强杀进程 (非优雅退出) ==="
    Stop-Server $s1
    $s1 = $null
    Write-Host "  已确认服务不再响应(端口已释放)"
    Get-ChildItem $paperDir -Filter *.json | ForEach-Object { Write-Host "    $($_.Name) ($($_.Length) bytes)" }

    Write-Host "`n=== 5. 重启服务 ==="
    $s2 = Start-Server "paper_e2e_p2.log"
    Write-Host "服务已就绪 (pid=$($s2.Id))"

    Write-Host "`n=== 6. 核对账户状态是否还在 ==="
    $after = Invoke-RestMethod "$base/api/paper/account?account_id=default"
    $tradesAfter = Invoke-RestMethod "$base/api/paper/trades?account_id=default&limit=5"
    $equityAfter = Invoke-RestMethod "$base/api/paper/equity?account_id=default"
    Write-Host "  重启后: 总资产=$($after.total_asset) 成交=$($tradesAfter.total) 交易日=$($after.trading_days)"
    Write-Host "  最新一笔: $($tradesAfter.trades[0].trade_id) @ $($tradesAfter.trades[0].executed_at)"

    $fail = @()
    if ($after.total_asset -ne $before.total_asset) { $fail += "总资产变了: $($before.total_asset) -> $($after.total_asset)" }
    if ($tradesAfter.total -ne $tradesBefore.total) { $fail += "成交数变了: $($tradesBefore.total) -> $($tradesAfter.total)" }
    if ($after.trading_days -ne $before.trading_days) { $fail += "交易日数变了" }
    if ($tradesAfter.trades[0].trade_id -ne $firstTradeId) { $fail += "最新成交对不上" }
    if ($equityAfter.count -ne $before.trading_days) { $fail += "资金曲线长度对不上" }
    if ($fail.Count -gt 0) { throw "状态丢失: $($fail -join '; ')" }
    Write-Host "  OK 账户/成交/资金曲线全部一致"

    Write-Host "`n=== 7. 核对定时任务是否自动恢复 ==="
    $schedAfter = Invoke-RestMethod "$base/api/paper/schedule?account_id=default"
    Write-Host "  running=$($schedAfter.running) next_run=$($schedAfter.next_run_time)"
    if ($schedAfter.running -ne $true) { throw "重启后定时任务没有恢复 —— 会静默停掉" }
    Write-Host "  OK 定时任务已自动接回"

    Write-Host "`n=== 8. 重启后继续推进(不得重放历史) ==="
    $run2 = Post-Json "$base/api/paper/run" @{ account_id = "default"; days = 2 }
    Write-Host "  第二轮: $($run2.from) ~ $($run2.to) (重启前已推进到 $($run.to))"
    if ($run2.from -le $run.to) { throw "重启后回到了 $($run2.from), 历史被重放" }

    $eq2 = Invoke-RestMethod "$base/api/paper/equity?account_id=default"
    $dates = $eq2.equity | ForEach-Object { $_.date }
    $dup = ($dates | Group-Object | Where-Object { $_.Count -gt 1 })
    if ($dup) { throw "资金曲线出现重复日期: $($dup.Name -join ',')" }
    Write-Host "  OK 继续往前走, 且资金曲线无重复日期 ($($eq2.count) 个点)"

    Write-Host "`n=== 9. 定时路径立即执行一次 ==="
    $now = Post-Json "$base/api/paper/schedule/run" @{ account_id = "default" }
    Write-Host "  结果: status=$($now.status) 说明=$($now.reason)$($now.error)"
    $st = Invoke-RestMethod "$base/api/paper/schedule?account_id=default"
    Write-Host "  调度状态: last_status=$($st.last_status) run=$($st.run_count) error=$($st.error_count) skipped=$($st.skipped_count)"
    Write-Host "            last_run_at=$($st.last_run_at)"
    # 非交易日(周末/节假日)返回 skipped 也是正确行为, 但**必须留痕**
    if ($st.last_status -eq "") { throw "立即执行没有留下任何记录 —— 静默" }

    Write-Host "`n=========== E2E 全部通过 ==========="
}
finally {
    foreach ($p in @($s1, $s2)) { Stop-Server $p }
    Remove-LeakedServers
    Write-Host "`n=== 清理 ==="
    Get-ChildItem $paperDir -Filter *.json -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -ne $keepHolidays } | Remove-Item -Force
    Get-ChildItem $paperDir -Filter *.tmp -ErrorAction SilentlyContinue | Remove-Item -Force
    Write-Host "已清理 data/paper/ (保留 $keepHolidays)"
    Write-Host "服务日志: $env:TEMP\paper_e2e_p1.log / paper_e2e_p2.log"
    Stop-Transcript | Out-Null
}
