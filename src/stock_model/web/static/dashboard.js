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