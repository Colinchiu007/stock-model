'use strict';

// ---------- 工具函数 ----------
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const num = (v, d = 2) =>
  (v === null || v === undefined || Number.isNaN(v)) ? '—' : Number(v).toFixed(d);

const pct = (v) =>
  (v === null || v === undefined) ? '—' : (Number(v) * 100).toFixed(0) + '%';

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) { /* ignore */ }
    throw new Error(detail);
  }
  return res.json();
}

function show(el, on) { el.classList.toggle('hidden', !on); }

// ---------- 标签页 ----------
document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach((t) => t.classList.remove('active'));
    document.querySelectorAll('.panel').forEach((p) => p.classList.remove('active'));
    tab.classList.add('active');
    $('panel-' + tab.dataset.tab).classList.add('active');
  });
});

// ---------- 健康检查 ----------
async function checkHealth() {
  try {
    const d = await api('/api/health');
    $('health-dot').className = 'dot ok';
    $('health-text').textContent = `服务正常 v${d.version}`;
  } catch (e) {
    $('health-dot').className = 'dot bad';
    $('health-text').textContent = '服务不可用';
  }
}
checkHealth();
setInterval(checkHealth, 30000);

// ============================================================
// 选股榜
// ============================================================
$('btn-scan').addEventListener('click', async () => {
  const symbols = $('sc-symbols').value.trim();
  if (!symbols) { alert('请输入股票代码'); return; }

  const btn = $('btn-scan');
  btn.disabled = true;
  show($('sc-loading'), true);
  show($('sc-summary'), false);

  const qs = new URLSearchParams({
    symbols,
    data_source: $('sc-source').value,
    start_date: $('sc-start').value.trim() || '20240101',
  });

  try {
    const data = await api('/api/screener?' + qs);
    renderScreener(data);
  } catch (e) {
    $('sc-body').innerHTML =
      `<tr class="empty-row"><td colspan="11">扫描失败：${esc(e.message)}</td></tr>`;
  } finally {
    btn.disabled = false;
    show($('sc-loading'), false);
  }
});

function renderScreener(data) {
  const s = data.summary || {};
  const ac = s.action_counts || {};

  $('sc-summary').innerHTML = `
    <div class="item"><span class="k">扫描标的</span><span class="v">${s.scanned ?? 0}</span></div>
    <div class="item"><span class="k">返回结果</span><span class="v">${s.total ?? 0}</span></div>
    <div class="item"><span class="k">建议买入</span><span class="v" style="color:var(--buy)">${ac.buy || 0}</span></div>
    <div class="item"><span class="k">建议卖出</span><span class="v" style="color:var(--sell)">${ac.sell || 0}</span></div>
    <div class="item"><span class="k">建议观望</span><span class="v" style="color:var(--hold)">${ac.hold || 0}</span></div>
    <div class="item"><span class="k">失败</span><span class="v" style="color:var(--warn)">${s.failed || 0}</span></div>
  `;
  show($('sc-summary'), true);

  const rows = data.results || [];
  if (!rows.length) {
    $('sc-body').innerHTML = '<tr class="empty-row"><td colspan="11">无结果</td></tr>';
    return;
  }

  $('sc-body').innerHTML = rows.map((r) => {
    const sc = r.score;
    const scCls = sc > 0 ? 'pos' : (sc < 0 ? 'neg' : 'zero');
    const trendCls = r.trend === 'up' ? 'trend-up' : (r.trend === 'down' ? 'trend-down' : 'muted');

    const sigs = (r.signals || []).slice(0, 4).map((sg) => {
      const cls = ['sig', sg.type, sg.strength === 'strong' ? 'strong' : ''].join(' ');
      return `<span class="${cls}" title="${esc(sg.reason)}">${esc(sg.type.toUpperCase())}·${esc(sg.strength)}</span>`;
    }).join('');
    const more = (r.signals || []).length > 4 ? `<span class="sig">+${r.signals.length - 4}</span>` : '';

    const errCell = r.error
      ? `<span class="muted" title="${esc(r.error)}">${esc(r.error.slice(0, 28))}</span>`
      : '';

    return `<tr>
      <td><strong>${esc(r.symbol)}</strong></td>
      <td><span class="badge ${esc(r.action)}">${esc(r.action.toUpperCase())}</span></td>
      <td><span class="score ${scCls}">${num(sc, 2)}</span></td>
      <td>${num(r.confidence, 2)}</td>
      <td class="num">${num(r.close, 2)}</td>
      <td class="num">${num(r.target_price, 2)}</td>
      <td class="num">${num(r.stop_loss, 2)}</td>
      <td class="num">${num(r.position_pct, 0)}</td>
      <td class="${trendCls}">${esc(r.trend || '—')}</td>
      <td><div class="sig-list">${sigs}${more}</div></td>
      <td>${errCell}</td>
    </tr>`;
  }).join('');
}

// ============================================================
// 个股分析
// ============================================================
$('btn-detail').addEventListener('click', async () => {
  const symbol = $('dt-symbol').value.trim();
  if (!symbol) { alert('请输入股票代码'); return; }

  $('btn-detail').disabled = true;
  show($('dt-loading'), true);
  $('dt-content').innerHTML = '';

  const qs = new URLSearchParams({ data_source: $('dt-source').value });

  try {
    const d = await api(`/api/analyze/${symbol}?` + qs);
    renderDetail(d);
  } catch (e) {
    $('dt-content').innerHTML = `<div class="alert">分析失败：${esc(e.message)}</div>`;
  } finally {
    $('btn-detail').disabled = false;
    show($('dt-loading'), false);
  }
});

function renderDetail(d) {
  if (d.error) {
    $('dt-content').innerHTML = `<div class="alert">${esc(d.symbol)}：${esc(d.error)}</div>`;
    return;
  }

  const a = d.analysis || {};
  const ind = d.indicators || {};
  const q = d.quality || {};

  const actionBadge = `<span class="badge ${esc(a.action || 'hold')}">${esc((a.action || 'hold').toUpperCase())}</span>`;

  const sigHtml = (a.signals || []).length
    ? (a.signals || []).map((s) => `
        <div class="kv-row">
          <span class="k"><span class="badge ${esc(s.type)}">${esc(s.type.toUpperCase())}</span> ${esc(s.strength)}</span>
          <span class="v">${esc(s.reason)}</span>
        </div>`).join('')
    : '<div class="kv-row"><span class="muted">当前无交叉信号</span></div>';

  const qIssues = (q.issues || []).length
    ? (q.issues || []).map((i) => `<li>· [${esc(i.severity)}] ${esc(i.message)}</li>`).join('')
    : '<li class="muted">· 无质量问题</li>';

  $('dt-content').innerHTML = `
    <div class="grid2">
      <div class="kv">
        <h4>策略结论 — ${esc(d.symbol)}</h4>
        <div class="kv-row"><span class="k">建议</span><span class="v">${actionBadge}</span></div>
        <div class="kv-row"><span class="k">信心度</span><span class="v">${num(a.confidence, 2)}</span></div>
        <div class="kv-row"><span class="k">建议仓位</span><span class="v">${num(a.position_pct, 0)}%</span></div>
        <div class="kv-row"><span class="k">目标价</span><span class="v">${num(a.target_price, 2)}</span></div>
        <div class="kv-row"><span class="k">止损价</span><span class="v">${num(a.stop_loss, 2)}</span></div>
        <div class="kv-row"><span class="k">最新收盘</span><span class="v">${num(a.close, 2)}</span></div>
        <div class="kv-row"><span class="k">趋势</span><span class="v">${esc(a.trend || '—')}</span></div>
        <div class="kv-row"><span class="k">理由</span><span class="v" style="font-size:12px">${esc(a.reason)}</span></div>
      </div>

      <div class="kv">
        <h4>技术指标快照</h4>
        <div class="kv-row"><span class="k">MA5 / MA20 / MA60</span>
          <span class="v">${num(ind.ma5, 2)} / ${num(ind.ma20, 2)} / ${num(ind.ma60, 2)}</span></div>
        <div class="kv-row"><span class="k">RSI(14)</span><span class="v">${num(ind.rsi14, 2)}</span></div>
        <div class="kv-row"><span class="k">MACD / DEA / 柱</span>
          <span class="v">${num(ind.macd, 3)} / ${num(ind.macd_signal, 3)} / ${num(ind.macd_hist, 3)}</span></div>
        <div class="kv-row"><span class="k">BOLL 上 / 中 / 下</span>
          <span class="v">${num(ind.boll_upper, 2)} / ${num(ind.boll_mid, 2)} / ${num(ind.boll_lower, 2)}</span></div>
        <div class="kv-row"><span class="k">ATR(14)</span><span class="v">${num(ind.atr14, 3)}</span></div>
        <div class="kv-row"><span class="k">KDJ K / D / J</span>
          <span class="v">${num(ind.kdj_k, 1)} / ${num(ind.kdj_d, 1)} / ${num(ind.kdj_j, 1)}</span></div>
        <div class="kv-row"><span class="k">OBV</span><span class="v">${num(ind.obv, 0)}</span></div>
      </div>

      <div class="kv">
        <h4>触发信号</h4>
        ${sigHtml}
      </div>

      <div class="kv">
        <h4>数据质量 — ${num(q.score, 0)}/100</h4>
        <ul style="font-size:12px;line-height:1.8">${qIssues}</ul>
      </div>
    </div>

    <h3 class="section-title">近 60 日收盘价</h3>
    ${sparkline(d.history || [])}
  `;
}

function sparkline(history) {
  if (!history || history.length < 2) return '<div class="muted">数据不足，无法绘制走势</div>';
  const vals = history.map((h) => h.close);
  const min = Math.min(...vals), max = Math.max(...vals);
  const span = (max - min) || 1;
  const W = 1000, H = 120, pad = 4;

  const pts = vals.map((v, i) => {
    const x = pad + (i / (vals.length - 1)) * (W - pad * 2);
    const y = H - pad - ((v - min) / span) * (H - pad * 2);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');

  const last = vals[vals.length - 1];
  const color = last >= vals[0] ? 'var(--buy)' : 'var(--sell)';
  const up = last >= vals[0];

  return `<div class="kv">
    <svg class="spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
      <polyline points="${pts}" fill="none" stroke="${color}" stroke-width="2"
                vector-effect="non-scaling-stroke"/>
    </svg>
    <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--text-dim)">
      <span>${esc(history[0].date)} · ${num(min, 2)}</span>
      <span style="color:${color}">${up ? '▲' : '▼'} ${num(last, 2)} (${(((last - vals[0]) / vals[0]) * 100).toFixed(2)}%)</span>
      <span>${esc(history[history.length - 1].date)} · ${num(max, 2)}</span>
    </div>
  </div>`;
}

// ============================================================
// 回测 / 交易记录
// ============================================================
$('btn-backtest').addEventListener('click', async () => {
  const symbol = $('bt-symbol').value.trim();
  if (!symbol) { alert('请输入股票代码'); return; }

  $('btn-backtest').disabled = true;
  show($('bt-loading'), true);
  $('bt-content').innerHTML = '';

  const qs = new URLSearchParams({
    data_source: $('bt-source').value,
    initial_cash: $('bt-cash').value || '100000',
  });

  try {
    const d = await api(`/api/backtest/${symbol}?` + qs);
    renderBacktest(d);
  } catch (e) {
    $('bt-content').innerHTML = `<div class="alert">回测失败：${esc(e.message)}</div>`;
  } finally {
    $('btn-backtest').disabled = false;
    show($('bt-loading'), false);
  }
});

function renderBacktest(d) {
  if (d.error) {
    $('bt-content').innerHTML = `<div class="alert">${esc(d.symbol)}：${esc(d.error)}</div>`;
    return;
  }

  const m = d.metrics || {};
  const ta_ = d.trade_analysis || {};
  const bm = d.benchmark || {};

  const trades = (d.trades || []).slice().reverse();
  const tradesHtml = trades.length
    ? trades.map((t) => `
        <tr>
          <td class="muted">${esc(t.timestamp)}</td>
          <td><span class="badge ${esc(t.action)}">${esc(t.action.toUpperCase())}</span></td>
          <td class="num">${num(t.price, 2)}</td>
          <td class="num">${t.shares}</td>
          <td class="num">${num(t.amount, 0)}</td>
          <td class="num">${num(t.commission, 2)}</td>
        </tr>`).join('')
    : '<tr class="empty-row"><td colspan="6">该区间内策略未产生任何交易</td></tr>';

  $('bt-content').innerHTML = `
    <div class="cards">
      <div class="card"><div class="card-label">总收益率</div>
        <div class="card-value" style="color:${d.total_return >= 0 ? 'var(--buy)' : 'var(--sell)'}">
          ${pct(d.total_return)}</div></div>
      <div class="card"><div class="card-label">夏普比率</div><div class="card-value">${num(m.sharpe, 2)}</div></div>
      <div class="card"><div class="card-label">最大回撤</div><div class="card-value">${pct(m.max_drawdown)}</div></div>
      <div class="card"><div class="card-label">胜率</div><div class="card-value">${pct(ta_.win_rate)}</div></div>
      <div class="card"><div class="card-label">交易笔数</div><div class="card-value">${d.trade_count}</div></div>
      <div class="card"><div class="card-label">Alpha</div><div class="card-value">${pct(bm.alpha)}</div></div>
    </div>

    <div class="grid2">
      <div class="kv">
        <h4>资金</h4>
        <div class="kv-row"><span class="k">初始资金</span><span class="v">${num(d.initial_cash, 0)}</span></div>
        <div class="kv-row"><span class="k">最终资金</span><span class="v">${num(d.final_cash, 0)}</span></div>
        <div class="kv-row"><span class="k">总佣金</span><span class="v">${num(m.total_commission, 2)}</span></div>
      </div>
      <div class="kv">
        <h4>交易统计</h4>
        <div class="kv-row"><span class="k">盈利笔数 / 亏损笔数</span>
          <span class="v">${ta_.win_count ?? 0} / ${ta_.loss_count ?? 0}</span></div>
        <div class="kv-row"><span class="k">平均持仓天数</span><span class="v">${num(ta_.avg_holding_days, 1)}</span></div>
        <div class="kv-row"><span class="k">盈利因子</span><span class="v">${num(ta_.profit_factor, 2)}</span></div>
        <div class="kv-row"><span class="k">基准(买入持有)收益</span><span class="v">${pct(bm.benchmark_return)}</span></div>
        <div class="kv-row"><span class="k">Beta</span><span class="v">${num(bm.beta, 3)}</span></div>
      </div>
    </div>

    <h3 class="section-title">交易记录（最近 ${trades.length} 笔，倒序）</h3>
    <table class="table">
      <thead><tr><th>时间</th><th>动作</th><th class="num">价格</th><th class="num">股数</th>
        <th class="num">金额</th><th class="num">佣金</th></tr></thead>
      <tbody>${tradesHtml}</tbody>
    </table>

    <h3 class="section-title">权益曲线</h3>
    ${sparkline((d.equity_curve || []).map((e) => ({ date: '#' + e.index, close: e.equity })))}
  `;
}

// ============================================================
// Pipeline
// ============================================================
async function refreshPipeline() {
  try {
    const s = await api('/api/pipeline/status');
    $('pl-status').textContent = s.status || '—';
    $('pl-runs').textContent = s.total_runs ?? 0;
    $('pl-symbols').textContent = (s.watchlist || []).length;

    const h = await api('/api/pipeline/history?limit=50');
    const rows = (h.history || []).slice().reverse();
    $('pl-body').innerHTML = rows.length
      ? rows.map((r) => `
          <tr>
            <td class="muted">${esc((r.timestamp || '').replace('T', ' ').slice(0, 19))}</td>
            <td><strong>${esc(r.symbol)}</strong></td>
            <td><span class="badge ${r.status === 'executed' ? 'buy' : 'hold'}">${esc(r.status)}</span></td>
            <td>${esc(r.action || '—')}</td>
            <td>${num(r.confidence, 2)}</td>
            <td class="num">${num(r.quality_score, 0)}</td>
            <td style="white-space:normal">${esc(r.reason || '')}</td>
          </tr>`).join('')
      : '<tr class="empty-row"><td colspan="7">暂无记录</td></tr>';
  } catch (e) { /* 静默 */ }
}

$('pl-start').addEventListener('click', async () => {
  const watchlist = $('pl-watchlist').value.split(',').map((s) => s.trim()).filter(Boolean);
  try {
    await api('/api/pipeline/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ interval_minutes: parseInt($('pl-interval').value, 10) || 30, watchlist }),
    });
    setTimeout(refreshPipeline, 600);
  } catch (e) { alert('启动失败：' + e.message); }
});

$('pl-stop').addEventListener('click', async () => {
  try { await api('/api/pipeline/stop', { method: 'POST' }); setTimeout(refreshPipeline, 400); }
  catch (e) { alert('停止失败：' + e.message); }
});

$('pl-run').addEventListener('click', async (e) => {
  e.target.disabled = true;
  try {
    await api('/api/pipeline/run', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}),
    });
  } catch (err) { alert('执行失败：' + err.message); }
  finally { setTimeout(() => { e.target.disabled = false; refreshPipeline(); }, 1500); }
});

refreshPipeline();
setInterval(() => {
  if ($('panel-pipeline').classList.contains('active')) refreshPipeline();
}, 10000);
// ============================================================
// 模拟盘 (Paper Trading)
// ============================================================

const PAPER = { busy: false };

// ---------- 定时运行 ----------
// 诚实性约定（与后端的花了心思保持一致）：
//   · 查不到状态 → 显示"状态不明"，不显示成"未开启"
//   · 缺 apscheduler → 后端返回 503，这里把原因写出来并**禁用按钮**
//   · 上次失败 → 把 last_error 原文显示出来，并带上连续失败次数
//   · 非交易日"立即执行"返回 skipped → 如实显示"未执行：非交易日"
// 一句话：宁可显示坏消息，也不要让人以为它在跑。
function _schedSetState(text, cls) {
  const el = $('pp-sched-state');
  el.textContent = text;
  el.className = 'badge ' + cls;
}

function _schedSetDisabled(disabled) {
  $('pp-sched-start').disabled = disabled;
  $('pp-sched-stop').disabled = disabled;
  $('pp-sched-run').disabled = disabled;
}

function _schedLastText(s) {
  if (!s.last_run_at) return '尚无执行记录';
  const when = new Date(s.last_run_at).toLocaleString();
  if (s.last_status === 'ok') {
    const r = s.last_result || {};
    return `上次 ${when} 成功 · ${r.steps || 0} 步 / ${r.trades || 0} 笔成交`;
  }
  if (s.last_status === 'skipped') return `上次 ${when} 跳过（非交易日）`;
  if (s.last_status === 'error') {
    const n = s.consecutive_failures || 0;
    return `上次 ${when} 失败（连续 ${n} 次）· ${s.last_error || '原因未知'}`;
  }
  return `上次 ${when}`;
}

function renderSchedule(s) {
  if (!s || s.running !== true) {
    _schedSetState('● 未开启', 'idle');
    $('pp-sched-plan').textContent = s && s.restore_error
      ? `定时任务未能恢复：${s.restore_error}`
      : '未开启 —— 开启后会在每个交易日按设定时间自动推进';
    $('pp-sched-last').textContent = '尚无执行记录';
    $('pp-sched-alert').textContent = '';
    $('pp-sched-warns').innerHTML = '';
    $('pp-sched-warns').classList.add('hidden');
    return;
  }

  _schedSetState('● 已开启', 'ok');
  const next = s.next_run_time ? ` · 下次 ${new Date(s.next_run_time).toLocaleString()}` : '';
  $('pp-sched-plan').textContent = `${s.schedule || '—'}${next}`;
  $('pp-sched-last').textContent = _schedLastText(s);
  $('pp-sched-alert').textContent =
    `告警通道：${s.alert_channel || '—'} · 已发 ${s.alert_count || 0} 条`;

  const box = $('pp-sched-warns');
  const warns = s.warnings || [];
  if (warns.length) {
    box.innerHTML = `<div class="warn-box"><div class="t">⚠️ 定时运行提示</div>
      <ul>${warns.map((w) => `<li>${esc(w)}</li>`).join('')}</ul></div>`;
    box.classList.remove('hidden');
  } else {
    box.innerHTML = '';
    box.classList.add('hidden');
  }
}

async function paperScheduleRefresh() {
  try {
    renderSchedule(await api('/api/paper/schedule?account_id=default'));
  } catch (e) {
    _schedSetState('● 状态不明', 'err');
    $('pp-sched-plan').textContent = `查询定时状态失败：${e.message}`;
    $('pp-sched-last').textContent = '';
    $('pp-sched-alert').textContent = '';
    $('pp-sched-warns').innerHTML = '';
    $('pp-sched-warns').classList.add('hidden');
  }
}

async function paperScheduleAction(path, opts, okMsg) {
  $('pp-sched-msg').textContent = '处理中…';
  try {
    const r = await api(path, opts);
    if (path.endsWith('/run')) {
      if (r.status === 'skipped') {
        $('pp-sched-msg').textContent = `未执行：${r.reason || '非交易日'}`;
      } else if (r.status === 'error') {
        $('pp-sched-msg').textContent = `执行失败：${r.error || '未知原因'}`;
      } else {
        $('pp-sched-msg').textContent = `已执行 ${r.steps || 0} 步`;
      }
    } else {
      $('pp-sched-msg').textContent = okMsg;
    }
  } catch (e) {
    $('pp-sched-msg').textContent = `失败：${e.message}`;
    if (/apscheduler/i.test(e.message)) {
      // 缺依赖是"用不了"，不是"操作失败" —— 按钮禁掉并写明怎么装
      _schedSetDisabled(true);
      _schedSetState('● 不可用', 'err');
      $('pp-sched-plan').textContent =
        '定时不可用：未安装 apscheduler（pip install "stock-model[schedule]"）';
    }
  }
  await paperScheduleRefresh();
}

$('pp-sched-start').addEventListener('click', () => {
  const parts = ($('pp-sched-time').value || '15:30').split(':');
  paperScheduleAction('/api/paper/schedule', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      account_id: 'default',
      hour: parseInt(parts[0], 10),
      minute: parseInt(parts[1], 10),
      days: 1,
    }),
  }, '定时已开启');
});

$('pp-sched-stop').addEventListener('click', () => {
  paperScheduleAction(
    '/api/paper/schedule?account_id=default', { method: 'DELETE' }, '定时已停止',
  );
});

$('pp-sched-run').addEventListener('click', () => {
  paperScheduleAction('/api/paper/schedule/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ account_id: 'default' }),
  }, '已执行');
});

async function paperRefresh() {
  try {
    const [acc, pos, trades, eq] = await Promise.all([
      api('/api/paper/account'),
      api('/api/paper/positions'),
      api('/api/paper/trades?limit=100'),
      api('/api/paper/equity'),
    ]);

    $('pp-asset').textContent = '¥' + num(acc.total_asset, 2);
    const r = acc.total_return;
    const retEl = $('pp-return');
    retEl.textContent = `${r >= 0 ? '+' : ''}${(r * 100).toFixed(2)}%`;
    retEl.style.color = r >= 0 ? 'var(--buy)' : 'var(--sell)';
    $('pp-cash').textContent = '¥' + num(acc.cash, 2);
    $('pp-mv').textContent = '¥' + num(acc.market_value, 2);
    $('pp-ratio').textContent = '仓位 ' + (acc.position_ratio * 100).toFixed(0) + '%';
    $('pp-trades').textContent = trades.total;

    renderPositions(pos.positions);
    renderPaperTrades(trades.trades);
    renderEquity(eq.equity);

    try {
      const m = await api('/api/paper/metrics');
      renderPaperMetrics(m);
    } catch (e) { /* 绩效依赖数据量, 失败不阻塞 */ }

    // 定时状态独立刷新: 它失败不该把账户数据也一起判为"加载失败"
    await paperScheduleRefresh();
  } catch (e) {
    $('pp-warnings').innerHTML =
      `<div class="alert">加载模拟盘数据失败：${esc(e.message)}</div>`;
    await paperScheduleRefresh();
  }
}

function renderPaperMetrics(m) {
  const x = m.metrics;
  const acc = m.account;

  // ⚠️ 可靠性警告: 必须显著展示, 不许只挑好看的数字
  let warn = '';
  if (x.warnings && x.warnings.length) {
    warn = `<div class="warn-box">
      <div class="t">⚠️ 样本量不足，指标不可作为策略有效性依据</div>
      <ul>${x.warnings.map((w) => `<li>${esc(w)}</li>`).join('')}</ul>
    </div>`;
  }
  $('pp-warnings').innerHTML = warn;

  const card = (k, v, cls = '') =>
    `<div class="kv-row"><span class="k">${k}</span><span class="v ${cls}">${v}</span></div>`;
  const pnlCls = (v) => (v >= 0 ? 'pnl-pos' : 'pnl-neg');

  const bm = x.benchmark_return;
  const alpha = x.alpha;

  $('pp-metrics').innerHTML = `
    <div class="kv">
      <h4>收益</h4>
      ${card('累计收益', `${(x.total_return * 100).toFixed(2)}%`, pnlCls(x.total_return))}
      ${card('年化收益', `${(x.annual_return * 100).toFixed(2)}%`, pnlCls(x.annual_return))}
      ${card('总资产', '¥' + num(acc.total_asset, 2))}
      ${card('交易天数', x.trading_days + ' 天')}
    </div>
    <div class="kv">
      <h4>风险</h4>
      ${card('最大回撤', (x.max_drawdown * 100).toFixed(2) + '%', 'pnl-neg')}
      ${card('回撤持续', x.max_drawdown_duration + ' 天')}
      ${card('年化波动', (x.volatility * 100).toFixed(2) + '%')}
      ${card('夏普比率', num(x.sharpe, 2), pnlCls(x.sharpe))}
    </div>
    <div class="kv">
      <h4>交易质量</h4>
      ${card('完整回合', x.round_trips + ' 次')}
      ${card('胜率', (x.win_rate * 100).toFixed(1) + '%')}
      ${card('盈亏比', num(x.profit_loss_ratio, 2))}
      ${card('总费用', '¥' + num(x.total_fee, 2))}
    </div>
    <div class="kv">
      <h4>对比买入持有</h4>
      ${card('基准收益', `${(bm * 100).toFixed(2)}%`, pnlCls(bm))}
      ${card('超额收益', `${(alpha * 100).toFixed(2)}%`, pnlCls(alpha))}
      ${card('Beta', num(x.beta, 3))}
      ${card('样本是否充足', x.reliable ? '是' : '否（见上方警告）')}
    </div>`;
}

function renderPositions(positions) {
  if (!positions || !positions.length) {
    $('pp-pos-body').innerHTML = '<tr class="empty-row"><td colspan="8">暂无持仓</td></tr>';
    return;
  }
  $('pp-pos-body').innerHTML = positions.map((p) => {
    const pnl = (p.profit_loss_pct || 0);
    const cls = pnl >= 0 ? 'pnl-pos' : 'pnl-neg';
    const frozen = p.frozen_shares > 0
      ? `<span class="frozen-tag">冻结${p.frozen_shares}(T+1)</span>` : '';
    return `<tr>
      <td><strong>${esc(p.symbol)}</strong></td>
      <td class="num">${p.shares}</td>
      <td class="num">${num(p.avg_cost, 3)}</td>
      <td class="num">${num(p.last_price, 2)}</td>
      <td class="num">${num(p.market_value, 0)}</td>
      <td class="num ${cls}">${num(p.profit_loss, 0)}</td>
      <td class="num ${cls}">${(pnl * 100).toFixed(2)}%</td>
      <td class="num">${p.available_shares ?? p.shares}${frozen}</td>
    </tr>`;
  }).join('');
}

function renderPaperTrades(trades) {
  if (!trades || !trades.length) {
    $('pp-trade-body').innerHTML = '<tr class="empty-row"><td colspan="8">暂无成交</td></tr>';
    return;
  }
  $('pp-trade-body').innerHTML = trades.map((t) => {
    const fee = (t.commission || 0) + (t.stamp_tax || 0) + (t.transfer_fee || 0);
    return `<tr>
      <td class="muted">${esc(t.executed_at)}</td>
      <td><strong>${esc(t.symbol)}</strong></td>
      <td><span class="badge ${esc(t.side)}">${esc(t.side.toUpperCase())}</span></td>
      <td class="num">${t.shares}</td>
      <td class="num">${num(t.price, 3)}</td>
      <td class="num">${num(t.amount, 0)}</td>
      <td class="num">${num(fee, 2)}</td>
      <td class="muted" style="font-size:11px">${esc(t.signal_source || '')} ${t.signal_confidence ? num(t.signal_confidence, 2) : ''}</td>
    </tr>`;
  }).join('');
}

function renderEquity(equity) {
  if (!equity || equity.length < 2) {
    $('pp-equity').innerHTML = '<div class="muted">数据不足，运行模拟后显示</div>';
    return;
  }
  $('pp-equity').innerHTML = sparkline(equity.map((p) => ({ date: p.date, close: p.total_asset })));
}

$('pp-run').addEventListener('click', async (e) => {
  if (PAPER.busy) return;
  PAPER.busy = true;
  e.target.disabled = true;
  show($('pp-loading'), true);
  try {
    const r = await api('/api/paper/run', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ days: parseInt($('pp-days').value, 10) }),
    });
    if (r.status === 'ok') {
      $('pp-range').textContent = `${r.from} ~ ${r.to} · ${r.steps} 个交易日 · ${r.trades} 笔成交`;
    } else {
      $('pp-range').textContent = '执行异常: ' + (r.error || '未知');
    }
  } catch (err) {
    $('pp-range').textContent = '失败: ' + err.message;
  } finally {
    PAPER.busy = false;
    e.target.disabled = false;
    show($('pp-loading'), false);
    paperRefresh();
  }
});

$('pp-refresh').addEventListener('click', paperRefresh);
$('pp-reset').addEventListener('click', async () => {
  if (!confirm('重置模拟账户？当前所有模拟持仓与成交记录将清空。')) return;
  await api('/api/paper/reset', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}),
  });
  $('pp-range').textContent = '账户已重置';
  paperRefresh();
});

paperRefresh();
