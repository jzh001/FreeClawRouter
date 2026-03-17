"""
FreeClaw – dashboard.py
Web dashboard served at http://localhost:8765/dashboard

Provides:
  GET /dashboard          → single-page HTML dashboard
  GET /api/dashboard-data → JSON payload consumed by the page's charts

The page auto-refreshes every 10 seconds without a full reload.
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse

from . import storage as _storage
from .rate_limiter import RateLimiterRegistry

router = APIRouter()

# Injected at startup by main.py
_registry: RateLimiterRegistry | None = None


def set_registry(reg: RateLimiterRegistry) -> None:
    global _registry
    _registry = reg


# ---------------------------------------------------------------------------
# Data endpoint
# ---------------------------------------------------------------------------

@router.get("/api/dashboard-data")
async def dashboard_data() -> JSONResponse:
    today   = _storage.get_today_stats()
    hourly  = _storage.get_hourly_series(24)
    daily   = _storage.get_daily_series(7)
    rl_stats = _registry.all_stats() if _registry else {}

    # Merge rate-limit live stats into provider info
    providers_out: dict[str, Any] = {}
    all_provider_names = set(today["providers"].keys()) | set(rl_stats.keys())
    for name in sorted(all_provider_names):
        providers_out[name] = {
            "today":      today["providers"].get(name, {"requests": 0, "tokens": 0, "errors": 0, "local_fallbacks": 0}),
            "rate_limits": rl_stats.get(name, {}),
        }

    return JSONResponse({
        "timestamp": int(time.time()),
        "providers": providers_out,
        "totals":    today["totals"],
        "hourly":    hourly,
        "daily":     daily,
    })


# ---------------------------------------------------------------------------
# Dashboard HTML
# ---------------------------------------------------------------------------

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FreeClaw Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #0f1117; --surface: #1a1d27; --border: #2a2d3a;
    --text: #e2e8f0; --muted: #8892a4; --accent: #6366f1;
    --green: #22c55e; --yellow: #eab308; --red: #ef4444; --blue: #3b82f6;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif; font-size: 14px; }
  header { background: var(--surface); border-bottom: 1px solid var(--border); padding: 14px 24px; display: flex; align-items: center; gap: 12px; }
  header h1 { font-size: 18px; font-weight: 600; }
  header .subtitle { color: var(--muted); font-size: 13px; }
  .refresh-badge { margin-left: auto; background: var(--border); border-radius: 6px; padding: 4px 10px; font-size: 12px; color: var(--muted); }
  .refresh-badge span { color: var(--accent); font-weight: 600; }
  main { padding: 20px 24px; max-width: 1400px; margin: 0 auto; }
  h2 { font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: .07em; color: var(--muted); margin-bottom: 12px; }

  /* Summary row */
  .summary { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 24px; }
  .stat-card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 16px; }
  .stat-card .label { font-size: 12px; color: var(--muted); margin-bottom: 6px; }
  .stat-card .value { font-size: 26px; font-weight: 700; }
  .stat-card .sub { font-size: 12px; color: var(--muted); margin-top: 4px; }

  /* Provider cards */
  .provider-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 12px; margin-bottom: 24px; }
  .provider-card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; }
  .provider-card .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
  .provider-card .name { font-weight: 600; font-size: 15px; }
  .status-dot { width: 10px; height: 10px; border-radius: 50%; }
  .status-green { background: var(--green); box-shadow: 0 0 6px var(--green); }
  .status-yellow { background: var(--yellow); box-shadow: 0 0 6px var(--yellow); }
  .status-red { background: var(--red); box-shadow: 0 0 6px var(--red); }
  .provider-card .metric { display: flex; justify-content: space-between; font-size: 13px; color: var(--muted); padding: 3px 0; border-bottom: 1px solid var(--border); }
  .provider-card .metric:last-child { border-bottom: none; }
  .provider-card .metric .val { color: var(--text); font-weight: 500; }
  .progress-bar { height: 4px; background: var(--border); border-radius: 2px; margin-top: 10px; overflow: hidden; }
  .progress-fill { height: 100%; border-radius: 2px; transition: width .4s; }

  /* Charts */
  .charts { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 24px; }
  .chart-card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 16px; }
  .chart-card canvas { max-height: 260px; }
  .chart-wide { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 16px; margin-bottom: 24px; }
  .chart-wide canvas { max-height: 220px; }

  @media (max-width: 900px) { .charts { grid-template-columns: 1fr; } .summary { grid-template-columns: 1fr 1fr; } }
</style>
</head>
<body>
<header>
  <div style="font-size:22px">🦅</div>
  <div>
    <h1>FreeClaw Dashboard</h1>
    <div class="subtitle">OpenAI-compatible reverse proxy · free-tier router</div>
  </div>
  <div class="refresh-badge">Auto-refresh in <span id="countdown">10</span>s</div>
</header>

<main>
  <!-- Summary row -->
  <h2>Today (UTC)</h2>
  <div class="summary">
    <div class="stat-card"><div class="label">Requests Today</div><div class="value" id="tot-req">—</div><div class="sub" id="tot-req-sub"></div></div>
    <div class="stat-card"><div class="label">Tokens Today</div><div class="value" id="tot-tok">—</div><div class="sub" id="tot-tok-sub"></div></div>
    <div class="stat-card"><div class="label">Errors</div><div class="value" id="tot-err">—</div><div class="sub">upstream failures</div></div>
    <div class="stat-card"><div class="label">Local Fallbacks</div><div class="value" id="tot-local">—</div><div class="sub">ran on local Ollama</div></div>
  </div>

  <!-- Provider status cards -->
  <h2>Provider Status</h2>
  <div class="provider-grid" id="provider-grid"></div>

  <!-- Charts row -->
  <div class="charts">
    <div class="chart-card">
      <h2>Requests Today — by Provider</h2>
      <canvas id="chart-today-req"></canvas>
    </div>
    <div class="chart-card">
      <h2>Tokens Today — by Provider</h2>
      <canvas id="chart-today-tok"></canvas>
    </div>
  </div>

  <!-- Hourly series -->
  <div class="chart-wide">
    <h2>Requests / Hour — Last 24 Hours</h2>
    <canvas id="chart-hourly"></canvas>
  </div>

  <!-- 7-day -->
  <div class="chart-wide">
    <h2>Requests / Day — Last 7 Days</h2>
    <canvas id="chart-daily"></canvas>
  </div>
</main>

<script>
// ---- helpers ---------------------------------------------------------------
const COLORS = [
  '#6366f1','#22c55e','#3b82f6','#f59e0b','#ec4899',
  '#14b8a6','#a855f7','#f97316','#06b6d4','#84cc16'
];
function fmt(n) {
  if (n === undefined || n === null) return '—';
  if (n >= 1_000_000) return (n/1_000_000).toFixed(1)+'M';
  if (n >= 1_000)     return (n/1_000).toFixed(1)+'K';
  return String(n);
}
function headPct(used, limit) {
  if (!limit) return null;
  return Math.max(0, Math.round((limit - used) / limit * 100));
}
function statusColor(pct) {
  if (pct === null) return 'status-green';
  if (pct < 10) return 'status-red';
  if (pct < 25) return 'status-yellow';
  return 'status-green';
}
function barColor(pct) {
  if (pct === null) return '#22c55e';
  if (pct < 10) return '#ef4444';
  if (pct < 25) return '#eab308';
  return '#22c55e';
}
function toLocalHour(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
}
function toLocalDay(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleDateString([], {month:'short', day:'numeric'});
}

// ---- chart instances -------------------------------------------------------
let chartTodayReq, chartTodayTok, chartHourly, chartDaily;
const ctx = id => document.getElementById(id).getContext('2d');
const baseOpts = {
  responsive:true, maintainAspectRatio:true,
  plugins:{legend:{labels:{color:'#8892a4',boxWidth:12,padding:10}}},
  scales:{
    x:{ticks:{color:'#8892a4'},grid:{color:'#2a2d3a'}},
    y:{ticks:{color:'#8892a4'},grid:{color:'#2a2d3a'},beginAtZero:true}
  }
};
function initCharts() {
  chartTodayReq = new Chart(ctx('chart-today-req'), {type:'bar', data:{labels:[],datasets:[]}, options:{...baseOpts, plugins:{legend:{display:false}}}});
  chartTodayTok = new Chart(ctx('chart-today-tok'), {type:'bar', data:{labels:[],datasets:[]}, options:{...baseOpts, plugins:{legend:{display:false}}}});
  chartHourly   = new Chart(ctx('chart-hourly'),    {type:'line',data:{labels:[],datasets:[]}, options:{...baseOpts, elements:{line:{tension:.4},point:{radius:2}}}});
  chartDaily    = new Chart(ctx('chart-daily'),     {type:'bar', data:{labels:[],datasets:[]}, options:{...baseOpts}});
}

// ---- update ----------------------------------------------------------------
function update(data) {
  // totals
  document.getElementById('tot-req').textContent   = fmt(data.totals.requests);
  document.getElementById('tot-tok').textContent   = fmt(data.totals.tokens);
  document.getElementById('tot-err').textContent   = fmt(data.totals.errors);
  document.getElementById('tot-local').textContent = fmt(data.totals.local_fallbacks);

  const ts = new Date(data.timestamp * 1000);
  document.getElementById('tot-req-sub').textContent = 'as of ' + ts.toLocaleTimeString();

  // provider cards
  const grid = document.getElementById('provider-grid');
  grid.innerHTML = '';
  const provNames = Object.keys(data.providers).sort();
  provNames.forEach((name, ci) => {
    const p = data.providers[name];
    const rl = p.rate_limits || {};
    const rpmPct = headPct(rl.rpm_used||0, rl.rpm_limit);
    const rpdPct = headPct(rl.rpd_used||0, rl.rpd_limit);
    const worstPct = [rpmPct, rpdPct].filter(x=>x!==null).reduce((a,b)=>Math.min(a,b), 100);
    const dotClass = statusColor(worstPct === 100 && rpmPct === null && rpdPct === null ? null : worstPct);
    const color = COLORS[ci % COLORS.length];

    const card = document.createElement('div');
    card.className = 'provider-card';
    card.innerHTML = `
      <div class="header">
        <span class="name" style="color:${color}">${name}</span>
        <span class="status-dot ${dotClass}"></span>
      </div>
      <div class="metric"><span>Req today</span><span class="val">${fmt(p.today.requests)}</span></div>
      <div class="metric"><span>Tokens today</span><span class="val">${fmt(p.today.tokens)}</span></div>
      <div class="metric"><span>RPM headroom</span><span class="val">${rpmPct !== null ? rpmPct+'%' : 'no cap'}</span></div>
      <div class="metric"><span>RPD headroom</span><span class="val">${rpdPct !== null ? rpdPct+'%' : 'no cap'}</span></div>
      <div class="metric"><span>Errors today</span><span class="val">${fmt(p.today.errors)}</span></div>
      <div class="progress-bar">
        <div class="progress-fill" style="width:${worstPct===100&&rpmPct===null&&rpdPct===null?100:worstPct||0}%; background:${barColor(worstPct===100&&rpmPct===null&&rpdPct===null?null:worstPct)}"></div>
      </div>`;
    grid.appendChild(card);
  });

  // today bar charts
  const todayLabels = provNames;
  const todayReqs   = provNames.map(n => data.providers[n].today.requests || 0);
  const todayToks   = provNames.map(n => data.providers[n].today.tokens   || 0);
  const barColors   = provNames.map((_,i) => COLORS[i % COLORS.length]);

  chartTodayReq.data = {labels: todayLabels, datasets:[{data: todayReqs, backgroundColor: barColors, borderRadius:4}]};
  chartTodayReq.update('none');

  chartTodayTok.data = {labels: todayLabels, datasets:[{data: todayToks, backgroundColor: barColors, borderRadius:4}]};
  chartTodayTok.update('none');

  // hourly line chart
  const hourlyBuckets = {};
  const providerSet = new Set();
  (data.hourly||[]).forEach(h => {
    providerSet.add(h.provider);
    if (!hourlyBuckets[h.hour_ts]) hourlyBuckets[h.hour_ts] = {};
    hourlyBuckets[h.hour_ts][h.provider] = h.requests;
  });
  const hourlyTs = Object.keys(hourlyBuckets).map(Number).sort();
  const hourlyProviders = [...providerSet].sort();
  chartHourly.data = {
    labels: hourlyTs.map(toLocalHour),
    datasets: hourlyProviders.map((pn, ci) => ({
      label: pn,
      data: hourlyTs.map(ts => hourlyBuckets[ts]?.[pn] || 0),
      borderColor: COLORS[ci % COLORS.length],
      backgroundColor: COLORS[ci % COLORS.length] + '33',
      fill: false,
    }))
  };
  chartHourly.update('none');

  // daily bar chart (stacked)
  const dailyBuckets = {};
  const dailyProviders = new Set();
  (data.daily||[]).forEach(d => {
    dailyProviders.add(d.provider);
    if (!dailyBuckets[d.day_ts]) dailyBuckets[d.day_ts] = {};
    dailyBuckets[d.day_ts][d.provider] = d.requests;
  });
  const dailyTs = Object.keys(dailyBuckets).map(Number).sort();
  const dailyProviderList = [...dailyProviders].sort();
  chartDaily.data = {
    labels: dailyTs.map(toLocalDay),
    datasets: dailyProviderList.map((pn, ci) => ({
      label: pn,
      data: dailyTs.map(ts => dailyBuckets[ts]?.[pn] || 0),
      backgroundColor: COLORS[ci % COLORS.length],
      borderRadius: 4,
      stack: 'stack',
    }))
  };
  chartDaily.options.scales.x.stacked = true;
  chartDaily.options.scales.y.stacked = true;
  chartDaily.update('none');
}

// ---- polling ---------------------------------------------------------------
let countdown = 10;
async function fetchAndUpdate() {
  try {
    const r = await fetch('/api/dashboard-data');
    const d = await r.json();
    update(d);
  } catch(e) {
    console.warn('Dashboard fetch failed:', e);
  }
}

function tick() {
  countdown--;
  document.getElementById('countdown').textContent = countdown;
  if (countdown <= 0) {
    countdown = 10;
    fetchAndUpdate();
  }
}

initCharts();
fetchAndUpdate();
setInterval(tick, 1000);
</script>
</body>
</html>"""


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    return HTMLResponse(_DASHBOARD_HTML)
