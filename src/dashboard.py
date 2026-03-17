"""
FreeClawRouter – dashboard.py
Web dashboard served at http://localhost:8765/dashboard

Provides:
  GET  /dashboard               → single-page HTML dashboard (Usage + Channels + Settings tabs)
  GET  /api/dashboard-data      → JSON payload consumed by the Usage tab's charts
  GET  /api/openclaw-channels   → current Telegram/WhatsApp config from openclaw.json
  POST /api/openclaw-channels   → save Telegram/WhatsApp config to openclaw.json

The page auto-refreshes every 10 seconds without a full reload.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import storage as _storage
from .rate_limiter import RateLimiterRegistry

router = APIRouter()

# Injected at startup by main.py
_registry: RateLimiterRegistry | None = None
_health_tracker = None  # HealthTracker instance, injected at startup


def set_registry(reg: RateLimiterRegistry) -> None:
    global _registry
    _registry = reg


def set_health_tracker(tracker) -> None:
    global _health_tracker
    _health_tracker = tracker


# ---------------------------------------------------------------------------
# OpenClaw config helpers
# ---------------------------------------------------------------------------

def _openclaw_config_path() -> Path:
    config_dir = Path(os.environ.get("OPENCLAW_CONFIG_DIR", "./openclaw_config"))
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir / "openclaw.json"


def _read_openclaw_config() -> dict:
    path = _openclaw_config_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _write_openclaw_config(data: dict) -> None:
    _openclaw_config_path().write_text(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# OpenClaw channels API
# ---------------------------------------------------------------------------

@router.get("/api/openclaw-channels")
async def get_openclaw_channels() -> JSONResponse:
    data = _read_openclaw_config()
    channels = data.get("channels", {})
    return JSONResponse({
        "telegram": channels.get("telegram", {}),
        "whatsapp": channels.get("whatsapp", {}),
    })


@router.post("/api/openclaw-channels")
async def save_openclaw_channels(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")

    data = _read_openclaw_config()
    if "channels" not in data:
        data["channels"] = {}

    for channel in ("telegram", "whatsapp"):
        cfg = body.get(channel)
        if cfg is None:
            continue
        if cfg:
            data["channels"][channel] = cfg
        else:
            data["channels"].pop(channel, None)

    try:
        _write_openclaw_config(data)
    except Exception as e:
        raise HTTPException(500, f"Could not write openclaw.json: {e}")

    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Usage data endpoint
# ---------------------------------------------------------------------------

@router.get("/api/dashboard-data")
async def dashboard_data() -> JSONResponse:
    today    = _storage.get_today_stats()
    hourly   = _storage.get_hourly_series(24)
    daily    = _storage.get_daily_series(7)
    rl_stats = _registry.all_stats() if _registry else {}
    health   = _health_tracker.get_all() if _health_tracker else {}

    providers_out: dict[str, Any] = {}
    all_provider_names = set(today["providers"].keys()) | set(rl_stats.keys()) | set(health.keys())
    for name in sorted(all_provider_names):
        providers_out[name] = {
            "today":       today["providers"].get(name, {"requests": 0, "tokens": 0, "errors": 0, "local_fallbacks": 0}),
            "rate_limits": rl_stats.get(name, {}),
            "health":      health.get(name, {"status": "idle", "last_seen_ok": None, "consecutive_errors": 0}),
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

_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FreeClawRouter</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg:#0f1117; --surface:#1a1d27; --border:#2a2d3a;
    --text:#e2e8f0; --muted:#8892a4; --accent:#6366f1;
    --green:#22c55e; --yellow:#eab308; --red:#ef4444; --gray:#6b7280;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;font-size:14px}
  a{color:var(--accent);text-decoration:none}
  a:hover{text-decoration:underline}

  /* Header */
  header{background:var(--surface);border-bottom:1px solid var(--border);padding:14px 24px;display:flex;align-items:center;gap:12px}
  header h1{font-size:18px;font-weight:600}
  .subtitle{color:var(--muted);font-size:13px}
  .refresh-badge{margin-left:auto;background:var(--border);border-radius:6px;padding:4px 10px;font-size:12px;color:var(--muted)}
  .refresh-badge span{color:var(--accent);font-weight:600}

  /* Tabs */
  .tab-bar{background:var(--surface);border-bottom:1px solid var(--border);padding:0 24px;display:flex;gap:4px}
  .tab-btn{background:none;border:none;color:var(--muted);font-size:14px;font-family:inherit;cursor:pointer;padding:12px 20px;border-bottom:2px solid transparent;transition:color .15s,border-color .15s}
  .tab-btn:hover{color:var(--text)}
  .tab-btn.active{color:var(--accent);border-bottom-color:var(--accent);font-weight:600}

  main{padding:20px 24px;max-width:1400px;margin:0 auto}
  h2{font-size:13px;font-weight:600;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);margin-bottom:12px}

  /* Stat cards */
  .summary{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:24px}
  .stat-card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px}
  .stat-card .label{font-size:12px;color:var(--muted);margin-bottom:6px}
  .stat-card .value{font-size:26px;font-weight:700}
  .stat-card .sub{font-size:12px;color:var(--muted);margin-top:4px}

  /* Provider cards */
  .provider-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px;margin-bottom:24px}
  .provider-card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px 16px}
  .provider-card .hdr{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
  .provider-card .pname{font-weight:600;font-size:15px}
  .dot{width:10px;height:10px;border-radius:50%}
  .dot-g{background:var(--green);box-shadow:0 0 6px var(--green)}
  .dot-y{background:var(--yellow);box-shadow:0 0 6px var(--yellow)}
  .dot-r{background:var(--red);box-shadow:0 0 6px var(--red)}
  .dot-gray{background:var(--gray)}
  .metric{display:flex;justify-content:space-between;font-size:13px;color:var(--muted);padding:3px 0;border-bottom:1px solid var(--border)}
  .metric:last-child{border-bottom:none}
  .mval{color:var(--text);font-weight:500}
  .pbar{height:4px;background:var(--border);border-radius:2px;margin-top:10px;overflow:hidden}
  .pfill{height:100%;border-radius:2px;transition:width .4s}

  /* Charts */
  .charts{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:24px}
  .chart-card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px}
  .chart-card canvas{max-height:260px}
  .chart-wide{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px;margin-bottom:24px}
  .chart-wide canvas{max-height:220px}

  /* ---- Channels tab ---- */
  .setup-card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:24px;margin-bottom:20px}
  .setup-card h3{font-size:17px;font-weight:700;margin-bottom:4px;display:flex;align-items:center;gap:10px}
  .setup-card .tagline{color:var(--muted);font-size:13px;margin-bottom:20px}
  .badge-on{display:inline-block;background:#163016;color:var(--green);border:1px solid #2a4a2a;border-radius:20px;font-size:11px;font-weight:600;padding:2px 10px}
  .badge-off{display:inline-block;background:#2a1a1a;color:#ef4444;border:1px solid #4a2a2a;border-radius:20px;font-size:11px;font-weight:600;padding:2px 10px}

  .step{display:flex;gap:14px;margin-bottom:18px;align-items:flex-start}
  .step-num{min-width:28px;height:28px;background:var(--accent);color:#fff;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:13px;flex-shrink:0;margin-top:1px}
  .step-body{flex:1}
  .step-body strong{display:block;font-size:14px;margin-bottom:4px}
  .step-body p{color:var(--muted);font-size:13px;line-height:1.6;margin-bottom:6px}

  /* Command copy blocks */
  .cmd-wrap{position:relative;margin:6px 0}
  .cmd-block{background:#0a0c12;border:1px solid var(--border);border-radius:7px;padding:10px 44px 10px 14px;font-family:'Consolas','SF Mono',monospace;font-size:12px;color:#a8b4c8;white-space:pre-wrap;line-height:1.5}
  .copy-btn{position:absolute;top:7px;right:8px;background:var(--border);border:none;color:var(--muted);border-radius:5px;padding:3px 8px;font-size:11px;cursor:pointer;font-family:inherit;transition:background .15s,color .15s}
  .copy-btn:hover{background:var(--accent);color:#fff}

  /* Form controls */
  .form-section{margin-top:4px}
  .field{margin-bottom:14px}
  .field label{display:block;font-size:12px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.05em;margin-bottom:5px}
  .field input,.field select,.field textarea{width:100%;background:var(--bg);border:1px solid var(--border);border-radius:7px;color:var(--text);font-family:inherit;font-size:14px;padding:9px 12px;outline:none;transition:border-color .15s}
  .field input:focus,.field select:focus,.field textarea:focus{border-color:var(--accent)}
  .field textarea{resize:vertical;min-height:80px;line-height:1.5}
  .field .hint{font-size:12px;color:var(--muted);margin-top:5px;line-height:1.5}
  .field-row{display:grid;grid-template-columns:1fr 1fr;gap:14px}
  .toggle-row{display:flex;align-items:center;gap:10px;margin-bottom:16px}
  .toggle-row input[type=checkbox]{width:18px;height:18px;accent-color:var(--accent);cursor:pointer;flex-shrink:0}
  .toggle-row label{font-size:14px;cursor:pointer}

  .btn-save{background:var(--accent);color:#fff;border:none;border-radius:8px;padding:11px 28px;font-size:14px;font-weight:600;font-family:inherit;cursor:pointer;transition:opacity .15s}
  .btn-save:hover{opacity:.85}
  .btn-save:disabled{opacity:.4;cursor:default}
  .save-msg{margin-top:10px;font-size:13px;min-height:18px}
  .ok{color:var(--green)}
  .err{color:var(--red)}

  .divider{border:none;border-top:1px solid var(--border);margin:20px 0}

  /* ---- Settings tab ---- */
  .settings-card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:24px;margin-bottom:20px;max-width:600px}
  .settings-card h3{font-size:17px;font-weight:700;margin-bottom:4px}
  .settings-card .tagline{color:var(--muted);font-size:13px;margin-bottom:20px}
  .settings-hint{color:var(--muted);font-size:13px;line-height:1.6;margin-top:8px}
  .ollama-status{display:flex;align-items:center;gap:8px;font-size:13px;margin-bottom:16px;padding:10px 14px;background:var(--bg);border:1px solid var(--border);border-radius:8px}
  .ollama-status .dot{flex-shrink:0}

  @media(max-width:900px){
    .charts{grid-template-columns:1fr}
    .summary{grid-template-columns:1fr 1fr}
    .field-row{grid-template-columns:1fr}
  }
</style>
</head>
<body>

<header>
  <div style="font-size:22px">&#128293;</div>
  <div>
    <h1>FreeClawRouter</h1>
    <div class="subtitle">Zero-cost AI router &mdash; free-tier LLM proxy</div>
  </div>
  <div class="refresh-badge" id="refresh-badge">Refreshes in <span id="countdown">10</span>s</div>
</header>

<nav class="tab-bar">
  <button class="tab-btn active" id="btn-usage"    onclick="switchTab('usage')">&#128202; Usage</button>
  <button class="tab-btn"        id="btn-channels" onclick="switchTab('channels')">&#128241; Messaging Apps</button>
  <button class="tab-btn"        id="btn-test"     onclick="switchTab('test')">&#129514; Test Models</button>
  <button class="tab-btn"        id="btn-settings" onclick="switchTab('settings')">&#9881;&#65039; Settings</button>
</nav>

<!-- ====================================================
     USAGE TAB
     ==================================================== -->
<div id="tab-usage">
<main>
  <h2>Today (UTC)</h2>
  <div class="summary">
    <div class="stat-card"><div class="label">Requests Today</div><div class="value" id="tot-req">&mdash;</div><div class="sub" id="tot-req-sub"></div></div>
    <div class="stat-card"><div class="label">Tokens Today</div><div class="value" id="tot-tok">&mdash;</div><div class="sub"></div></div>
    <div class="stat-card"><div class="label">Errors</div><div class="value" id="tot-err">&mdash;</div><div class="sub">upstream failures</div></div>
    <div class="stat-card"><div class="label">Local Fallbacks</div><div class="value" id="tot-local">&mdash;</div><div class="sub">ran on Ollama</div></div>
  </div>

  <h2>Provider Status</h2>
  <div class="provider-grid" id="provider-grid"></div>

  <div class="charts">
    <div class="chart-card"><h2>Requests Today</h2><canvas id="chart-today-req"></canvas></div>
    <div class="chart-card"><h2>Tokens Today</h2><canvas id="chart-today-tok"></canvas></div>
  </div>
  <div class="chart-wide"><h2>Requests per Hour &mdash; Last 24 h</h2><canvas id="chart-hourly"></canvas></div>
  <div class="chart-wide"><h2>Requests per Day &mdash; Last 7 Days</h2><canvas id="chart-daily"></canvas></div>
</main>
</div>

<!-- ====================================================
     CHANNELS TAB
     ==================================================== -->
<div id="tab-channels" style="display:none">
<main>

  <!-- ---- Telegram ---- -->
  <div class="setup-card">
    <h3>
      &#128241; Telegram
      <span id="tg-badge" class="badge-off">Not configured</span>
    </h3>
    <p class="tagline">Chat with your AI assistant directly in Telegram &mdash; the easiest option to set up.</p>

    <!-- Step 1 -->
    <div class="step">
      <div class="step-num">1</div>
      <div class="step-body">
        <strong>Create a Telegram bot</strong>
        <p>Open Telegram, search for <strong>@BotFather</strong>, and send the message <code>/newbot</code>.
        Follow the prompts &mdash; it will give you a token that looks like <code>123456789:ABCdef...</code>.</p>
      </div>
    </div>

    <!-- Step 2 -->
    <div class="step">
      <div class="step-num">2</div>
      <div class="step-body">
        <strong>Enter your bot token and settings below, then click Save</strong>
        <div class="form-section">
          <div class="field">
            <label>Bot Token (from @BotFather)</label>
            <input type="password" id="tg-token" placeholder="123456789:ABCdef..." autocomplete="off">
            <div class="hint">Keep this private &mdash; it controls your bot.</div>
          </div>
          <div class="field-row">
            <div class="field">
              <label>Who can message your bot?</label>
              <select id="tg-dm-policy">
                <option value="pairing">Anyone with an approval code (recommended)</option>
                <option value="allowlist">Only specific people I approve</option>
                <option value="open">Anyone, no approval needed</option>
                <option value="disabled">Nobody (disable Telegram)</option>
              </select>
              <div class="hint">Use "approval code" to share with trusted friends while staying safe.</div>
            </div>
            <div class="field">
              <label>Allow in group chats?</label>
              <select id="tg-group-policy">
                <option value="disabled">No, DMs only</option>
                <option value="open">Yes, respond in any group</option>
              </select>
            </div>
          </div>
          <div class="field" id="tg-allowlist-field" style="display:none">
            <label>Approved Telegram User IDs</label>
            <textarea id="tg-allow-from" placeholder="Enter one user ID per line&#10;&#10;To find your ID, message @userinfobot in Telegram."></textarea>
            <div class="hint">Enter the numeric ID of each person you want to allow. Find yours by messaging @userinfobot in Telegram.</div>
          </div>
          <div class="toggle-row">
            <input type="checkbox" id="tg-enabled" checked>
            <label for="tg-enabled">Enable Telegram</label>
          </div>
          <button class="btn-save" onclick="saveTelegram()">Save Telegram Settings</button>
          <div class="save-msg" id="tg-save-msg"></div>
        </div>
      </div>
    </div>

    <!-- Step 3 -->
    <div class="step">
      <div class="step-num">3</div>
      <div class="step-body">
        <strong>Restart OpenClaw to apply the settings</strong>
        <p>Run this command in your terminal (in the FreeClawRouter folder):</p>
        <div class="cmd-wrap">
          <div class="cmd-block" id="tg-restart-cmd">docker compose restart openclaw</div>
          <button class="copy-btn" onclick="copyCmd('tg-restart-cmd',this)">Copy</button>
        </div>
      </div>
    </div>

    <!-- Step 4 -->
    <div class="step" id="tg-pairing-step">
      <div class="step-num">4</div>
      <div class="step-body">
        <strong>Approve your first message (for "approval code" mode)</strong>
        <p>Send any message to your bot in Telegram. It will reply with a short approval code.
        Then run these two commands to approve it:</p>
        <div class="cmd-wrap">
          <div class="cmd-block">docker exec freeclawrouter_openclaw openclaw pairing list telegram</div>
          <button class="copy-btn" onclick="copyCmd(this.previousElementSibling,this)">Copy</button>
        </div>
        <p style="margin-top:8px">Then replace <code>CODE</code> with the code shown above:</p>
        <div class="cmd-wrap">
          <div class="cmd-block">docker exec freeclawrouter_openclaw openclaw pairing approve telegram CODE</div>
          <button class="copy-btn" onclick="copyCmd(this.previousElementSibling,this)">Copy</button>
        </div>
        <p style="margin-top:8px;color:var(--muted);font-size:12px">Codes expire after 1 hour. You only need to do this once per person.</p>
      </div>
    </div>
  </div>

  <!-- ---- WhatsApp ---- -->
  <div class="setup-card">
    <h3>
      &#128241; WhatsApp
      <span id="wa-badge" class="badge-off">Not configured</span>
    </h3>
    <p class="tagline">Connect via WhatsApp &mdash; no special account needed, just a phone number.</p>

    <!-- Step 1 -->
    <div class="step">
      <div class="step-num">1</div>
      <div class="step-body">
        <strong>Link your phone number (do this first, before anything else)</strong>
        <p>We recommend using a <strong>dedicated phone number</strong>, not your personal WhatsApp.
        Run this command and scan the QR code that appears, just like you would when opening
        WhatsApp Web on a computer:</p>
        <div class="cmd-wrap">
          <div class="cmd-block">docker exec -it freeclawrouter_openclaw openclaw channels login --channel whatsapp</div>
          <button class="copy-btn" onclick="copyCmd(this.previousElementSibling,this)">Copy</button>
        </div>
        <p style="margin-top:8px;color:var(--muted);font-size:12px">
          On your phone: WhatsApp &rarr; Settings &rarr; Linked Devices &rarr; Link a Device
        </p>
      </div>
    </div>

    <!-- Step 2 -->
    <div class="step">
      <div class="step-num">2</div>
      <div class="step-body">
        <strong>Choose your privacy settings below, then click Save</strong>
        <div class="form-section">
          <div class="field-row">
            <div class="field">
              <label>Who can message your assistant?</label>
              <select id="wa-dm-policy">
                <option value="pairing">Anyone with an approval code (recommended)</option>
                <option value="allowlist">Only specific phone numbers I approve</option>
                <option value="open">Anyone, no approval needed</option>
                <option value="disabled">Nobody (disable WhatsApp)</option>
              </select>
            </div>
            <div class="field">
              <label>Allow in group chats?</label>
              <select id="wa-group-policy">
                <option value="disabled">No, messages only</option>
                <option value="open">Yes, respond in any group</option>
              </select>
            </div>
          </div>
          <div class="field" id="wa-allowlist-field" style="display:none">
            <label>Approved Phone Numbers</label>
            <textarea id="wa-allow-from" placeholder="Enter one phone number per line, including country code&#10;Example: +15551234567"></textarea>
            <div class="hint">Include the country code with a + sign (e.g. +65 for Singapore, +1 for USA).</div>
          </div>
          <button class="btn-save" onclick="saveWhatsApp()">Save WhatsApp Settings</button>
          <div class="save-msg" id="wa-save-msg"></div>
        </div>
      </div>
    </div>

    <!-- Step 3 -->
    <div class="step">
      <div class="step-num">3</div>
      <div class="step-body">
        <strong>Restart OpenClaw to apply the settings</strong>
        <div class="cmd-wrap">
          <div class="cmd-block">docker compose restart openclaw</div>
          <button class="copy-btn" onclick="copyCmd(this.previousElementSibling,this)">Copy</button>
        </div>
      </div>
    </div>

    <!-- Step 4 -->
    <div class="step">
      <div class="step-num">4</div>
      <div class="step-body">
        <strong>Approve your first message (for "approval code" mode)</strong>
        <p>Send any message to the linked number. Then approve the code:</p>
        <div class="cmd-wrap">
          <div class="cmd-block">docker exec freeclawrouter_openclaw openclaw pairing list whatsapp</div>
          <button class="copy-btn" onclick="copyCmd(this.previousElementSibling,this)">Copy</button>
        </div>
        <p style="margin-top:8px">Replace <code>CODE</code> with the code shown:</p>
        <div class="cmd-wrap">
          <div class="cmd-block">docker exec freeclawrouter_openclaw openclaw pairing approve whatsapp CODE</div>
          <button class="copy-btn" onclick="copyCmd(this.previousElementSibling,this)">Copy</button>
        </div>
      </div>
    </div>
  </div>

</main>
</div><!-- end channels tab -->

<!-- ====================================================
     SETTINGS TAB
     ==================================================== -->
<div id="tab-settings" style="display:none">
<main>
  <div class="settings-card">
    <h3>Local AI Routing</h3>
    <p class="tagline">Choose when to use your local AI model instead of cloud APIs.</p>

    <div class="field">
      <label>Routing preference</label>
      <select id="settings-threshold">
        <option value="disabled">Always use cloud APIs</option>
        <option value="simple">Use local AI for quick, simple tasks (recommended)</option>
        <option value="moderate">Use local AI for most tasks</option>
        <option value="always">Always use local AI (offline mode)</option>
      </select>
      <div class="settings-hint" id="settings-hint-text">
        Cloud APIs will be used for all requests. Your local Ollama model is only used as a fallback when all cloud quotas are exhausted.
      </div>
    </div>

    <button class="btn-save" onclick="saveSettings()">Save Settings</button>
    <div class="save-msg" id="settings-save-msg"></div>
  </div>

  <div class="settings-card">
    <h3>Local AI Model (Ollama)</h3>
    <p class="tagline">Choose which local model Ollama uses for fallback and routing decisions. Ollama is optional — cloud APIs work without it.</p>

    <div class="ollama-status" id="ollama-status-row">
      <span class="dot dot-gray" id="ollama-dot"></span>
      <span id="ollama-status-text" style="color:var(--muted)">Checking Ollama&hellip;</span>
    </div>

    <div class="field">
      <label>Local model</label>
      <select id="local-model-select">
        <option value="phi4-mini">phi4-mini (default — fast &amp; capable, ~2.5 GB)</option>
        <option value="gpt-oss:20b">gpt-oss:20b — best reasoning quality (~12 GB)</option>
        <option value="qwen3.5:9b">qwen3.5:9b — capable mid-size model (~5 GB)</option>
        <option value="qwen3.5:4b">qwen3.5:4b — balanced speed &amp; quality (~2.5 GB)</option>
        <option value="qwen3.5:2b">qwen3.5:2b — lightweight, very fast (~1.5 GB)</option>
        <option value="qwen3.5:0.8b">qwen3.5:0.8b — minimal, fastest (~0.5 GB)</option>
        <option value="none">None — disable local fallback</option>
      </select>
      <div class="hint" style="margin-top:6px">
        Pull the chosen model with: <code>ollama pull &lt;model&gt;</code>
      </div>
    </div>

    <button class="btn-save" onclick="saveLocalModel()">Save Local Model</button>
    <div class="save-msg" id="local-model-save-msg"></div>
  </div>
</main>
</div><!-- end settings tab -->

<!-- ====================================================
     TEST MODELS TAB
     ==================================================== -->
<div id="tab-test" style="display:none">
<main>
  <div class="settings-card" style="max-width:900px">
    <h2 style="margin:0 0 8px">Cloud Model Connectivity Test</h2>
    <p style="color:var(--muted);margin:0 0 20px">Sends a one-shot &ldquo;Reply with OK&rdquo; request to every configured cloud model in parallel (20 s timeout each).</p>
    <button class="btn-save" id="test-run-btn" onclick="runModelTests()" style="margin-bottom:24px">&#9654; Run Tests</button>
    <div id="test-status" style="color:var(--muted);font-size:13px;margin-bottom:16px"></div>
    <table id="test-table" style="display:none;width:100%;border-collapse:collapse;font-size:13px">
      <thead>
        <tr style="border-bottom:1px solid var(--border);color:var(--muted)">
          <th style="text-align:left;padding:8px 12px">Provider</th>
          <th style="text-align:left;padding:8px 12px">Model</th>
          <th style="text-align:center;padding:8px 12px">Status</th>
          <th style="text-align:right;padding:8px 12px">Latency</th>
          <th style="text-align:left;padding:8px 12px">Response / Error</th>
        </tr>
      </thead>
      <tbody id="test-tbody"></tbody>
    </table>
    <div id="test-summary" style="margin-top:16px;font-weight:600;font-size:14px"></div>
  </div>

  <div class="settings-card" style="max-width:900px;margin-top:20px">
    <h2 style="margin:0 0 8px">Local Model Test (Ollama)</h2>
    <p style="color:var(--muted);margin:0 0 20px">Tests the local Ollama model with a 60-second timeout. The first call may be slow if the model needs to be loaded into memory.</p>
    <button class="btn-save" id="local-test-btn" onclick="runLocalTest()" style="margin-bottom:24px">&#9654; Test Local Model</button>
    <div id="local-test-status" style="color:var(--muted);font-size:13px;margin-bottom:16px"></div>
    <table id="local-test-table" style="display:none;width:100%;border-collapse:collapse;font-size:13px">
      <thead>
        <tr style="border-bottom:1px solid var(--border);color:var(--muted)">
          <th style="text-align:left;padding:8px 12px">Provider</th>
          <th style="text-align:left;padding:8px 12px">Model</th>
          <th style="text-align:center;padding:8px 12px">Status</th>
          <th style="text-align:right;padding:8px 12px">Latency</th>
          <th style="text-align:left;padding:8px 12px">Response / Error</th>
        </tr>
      </thead>
      <tbody id="local-test-tbody"></tbody>
    </table>
    <div id="local-test-summary" style="margin-top:16px;font-weight:600;font-size:14px"></div>
  </div>
</main>
</div><!-- end test tab -->

<script>
// ============================================================
// Tab switching
// ============================================================
function switchTab(name) {
  document.getElementById('tab-usage').style.display    = name==='usage'    ? '' : 'none';
  document.getElementById('tab-channels').style.display = name==='channels' ? '' : 'none';
  document.getElementById('tab-test').style.display     = name==='test'     ? '' : 'none';
  document.getElementById('tab-settings').style.display = name==='settings' ? '' : 'none';
  document.getElementById('btn-usage').classList.toggle('active',    name==='usage');
  document.getElementById('btn-channels').classList.toggle('active', name==='channels');
  document.getElementById('btn-test').classList.toggle('active',     name==='test');
  document.getElementById('btn-settings').classList.toggle('active', name==='settings');
  document.getElementById('refresh-badge').style.display = name==='usage' ? '' : 'none';
  if (name === 'channels') loadChannels();
  if (name === 'settings') loadSettings();
}

// ============================================================
// Model tests — shared row renderer
// ============================================================
function _renderTestRows(tbody, results) {
  results.forEach(r => {
    const ok   = r.ok;
    const dot  = ok ? '\u2705' : '\u274c';
    const lat  = r.latency_ms != null ? r.latency_ms + ' ms' : '\u2014';
    const info = ok ? (r.response || '') : (r.error || 'unknown error');
    const row  = document.createElement('tr');
    row.style.borderBottom = '1px solid var(--border)';
    row.innerHTML = `
      <td style="padding:8px 12px">${r.provider || ''}</td>
      <td style="padding:8px 12px;font-family:monospace;font-size:12px">${r.model || ''}</td>
      <td style="padding:8px 12px;text-align:center">${dot}</td>
      <td style="padding:8px 12px;text-align:right;color:var(--muted)">${lat}</td>
      <td style="padding:8px 12px;color:${ok ? 'var(--text)' : '#e57373'};max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${info}">${info}</td>`;
    tbody.appendChild(row);
  });
}

async function runModelTests() {
  const btn    = document.getElementById('test-run-btn');
  const status = document.getElementById('test-status');
  const table  = document.getElementById('test-table');
  const tbody  = document.getElementById('test-tbody');
  const summary= document.getElementById('test-summary');

  btn.disabled = true;
  btn.textContent = 'Running\u2026';
  status.textContent = 'Sending test requests to all cloud models in parallel\u2026 (up to 20 s)';
  table.style.display = 'none';
  tbody.innerHTML = '';
  summary.textContent = '';

  try {
    const resp = await fetch('/api/test-models', {method:'POST'});
    const data = await resp.json();
    _renderTestRows(tbody, data.results);
    table.style.display = '';
    const p = data.passed, t = data.total;
    const color = p === t ? 'var(--green)' : p === 0 ? '#e57373' : '#ffb74d';
    summary.style.color = color;
    summary.textContent = `${p} / ${t} models passed`;
    status.textContent = '';
  } catch(e) {
    status.textContent = 'Test failed: ' + e;
  } finally {
    btn.disabled = false;
    btn.textContent = '\u25b6 Run Tests';
  }
}

async function runLocalTest() {
  const btn    = document.getElementById('local-test-btn');
  const status = document.getElementById('local-test-status');
  const table  = document.getElementById('local-test-table');
  const tbody  = document.getElementById('local-test-tbody');
  const summary= document.getElementById('local-test-summary');

  btn.disabled = true;
  btn.textContent = 'Testing\u2026';
  status.textContent = 'Waiting for local Ollama model\u2026 (up to 60 s \u2014 first call may be slow while the model loads)';
  table.style.display = 'none';
  tbody.innerHTML = '';
  summary.textContent = '';

  try {
    const resp = await fetch('/api/test-local', {method:'POST'});
    const data = await resp.json();
    if (data.note) {
      status.textContent = data.note;
    } else {
      _renderTestRows(tbody, data.results);
      table.style.display = '';
      const p = data.passed, t = data.total;
      const color = p === t ? 'var(--green)' : '#e57373';
      summary.style.color = color;
      summary.textContent = p === t ? 'Local model OK' : 'Local model failed';
      status.textContent = '';
    }
  } catch(e) {
    status.textContent = 'Test failed: ' + e;
  } finally {
    btn.disabled = false;
    btn.textContent = '\u25b6 Test Local Model';
  }
}

// ============================================================
// Copy button helper
// ============================================================
function copyCmd(elOrId, btn) {
  const el = typeof elOrId === 'string' ? document.getElementById(elOrId) : elOrId;
  navigator.clipboard.writeText(el.textContent.trim()).then(() => {
    const orig = btn.textContent;
    btn.textContent = 'Copied!';
    btn.style.background = 'var(--green)';
    btn.style.color = '#fff';
    setTimeout(() => { btn.textContent = orig; btn.style.background=''; btn.style.color=''; }, 1500);
  });
}

// ============================================================
// Show/hide allowlist fields based on policy selection
// ============================================================
document.getElementById('tg-dm-policy').addEventListener('change', function() {
  document.getElementById('tg-allowlist-field').style.display = this.value==='allowlist' ? '' : 'none';
  document.getElementById('tg-pairing-step').style.display    = this.value==='pairing'   ? '' : 'none';
});
document.getElementById('wa-dm-policy').addEventListener('change', function() {
  document.getElementById('wa-allowlist-field').style.display = this.value==='allowlist' ? '' : 'none';
});

// ============================================================
// Load current channel config
// ============================================================
async function loadChannels() {
  try {
    const r = await fetch('/api/openclaw-channels');
    const d = await r.json();
    applyTelegram(d.telegram || {});
    applyWhatsApp(d.whatsapp || {});
  } catch(e) { console.warn('Could not load channels:', e); }
}

function applyTelegram(tg) {
  document.getElementById('tg-token').value        = tg.botToken || '';
  const dmPol = tg.dmPolicy || 'pairing';
  document.getElementById('tg-dm-policy').value    = dmPol;
  document.getElementById('tg-group-policy').value = tg.groupPolicy || 'disabled';
  document.getElementById('tg-enabled').checked    = tg.enabled !== false;
  document.getElementById('tg-allow-from').value   = (tg.allowFrom||[]).join('\n');
  document.getElementById('tg-allowlist-field').style.display = dmPol==='allowlist' ? '' : 'none';
  document.getElementById('tg-pairing-step').style.display    = dmPol==='pairing'   ? '' : 'none';
  const configured = !!tg.botToken;
  document.getElementById('tg-badge').textContent  = configured ? 'Configured' : 'Not configured';
  document.getElementById('tg-badge').className    = configured ? 'badge-on' : 'badge-off';
}

function applyWhatsApp(wa) {
  const dmPol = wa.dmPolicy || 'pairing';
  document.getElementById('wa-dm-policy').value    = dmPol;
  document.getElementById('wa-group-policy').value = wa.groupPolicy || 'disabled';
  document.getElementById('wa-allow-from').value   = (wa.allowFrom||[]).join('\n');
  document.getElementById('wa-allowlist-field').style.display = dmPol==='allowlist' ? '' : 'none';
  const configured = !!wa.dmPolicy;
  document.getElementById('wa-badge').textContent  = configured ? 'Configured' : 'Not configured';
  document.getElementById('wa-badge').className    = configured ? 'badge-on' : 'badge-off';
}

function parseLines(text) {
  return text.split(/[\n,]+/).map(s=>s.trim()).filter(Boolean);
}

// ============================================================
// Save Telegram
// ============================================================
async function saveTelegram() {
  const btn = document.querySelector('#tab-channels .setup-card:nth-child(1) .btn-save');
  const msg = document.getElementById('tg-save-msg');
  const token = document.getElementById('tg-token').value.trim();
  if (!token) { msg.textContent='Please enter your bot token first.'; msg.className='save-msg err'; return; }

  btn.disabled = true; msg.textContent='Saving…'; msg.className='save-msg';

  const dmPolicy = document.getElementById('tg-dm-policy').value;
  const raw = parseLines(document.getElementById('tg-allow-from').value);
  const cfg = {
    enabled:     document.getElementById('tg-enabled').checked,
    botToken:    token,
    dmPolicy,
    groupPolicy: document.getElementById('tg-group-policy').value,
    allowFrom:   raw.map(v => isNaN(v) ? v : Number(v)),
  };
  if (!cfg.allowFrom.length) delete cfg.allowFrom;

  try {
    const r = await fetch('/api/openclaw-channels', {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({telegram:cfg})
    });
    if (!r.ok) throw new Error(await r.text());
    msg.innerHTML = '&#10003; Saved! Now restart OpenClaw: <code>docker compose restart openclaw</code>';
    msg.className = 'save-msg ok';
    applyTelegram(cfg);
  } catch(e) {
    msg.textContent = 'Error saving: ' + e.message;
    msg.className = 'save-msg err';
  }
  btn.disabled = false;
}

// ============================================================
// Save WhatsApp
// ============================================================
async function saveWhatsApp() {
  const btn = document.querySelector('#tab-channels .setup-card:nth-child(2) .btn-save');
  const msg = document.getElementById('wa-save-msg');
  btn.disabled = true; msg.textContent='Saving…'; msg.className='save-msg';

  const dmPolicy = document.getElementById('wa-dm-policy').value;
  const cfg = {
    dmPolicy,
    groupPolicy: document.getElementById('wa-group-policy').value,
    allowFrom:   parseLines(document.getElementById('wa-allow-from').value),
  };
  if (!cfg.allowFrom.length) delete cfg.allowFrom;

  try {
    const r = await fetch('/api/openclaw-channels', {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({whatsapp:cfg})
    });
    if (!r.ok) throw new Error(await r.text());
    msg.innerHTML = '&#10003; Saved! Now restart OpenClaw: <code>docker compose restart openclaw</code>';
    msg.className = 'save-msg ok';
    applyWhatsApp(cfg);
  } catch(e) {
    msg.textContent = 'Error saving: ' + e.message;
    msg.className = 'save-msg err';
  }
  btn.disabled = false;
}

// ============================================================
// Usage charts
// ============================================================
const COLORS=['#6366f1','#22c55e','#3b82f6','#f59e0b','#ec4899','#14b8a6','#a855f7','#f97316','#06b6d4','#84cc16'];
function fmt(n){if(n===undefined||n===null)return'—';if(n>=1e6)return(n/1e6).toFixed(1)+'M';if(n>=1e3)return(n/1e3).toFixed(1)+'K';return String(n)}
function headPct(used,lim){if(!lim)return null;return Math.max(0,Math.round((lim-used)/lim*100))}
function dotClass(p){if(p===null)return'dot dot-g';if(p<10)return'dot dot-r';if(p<25)return'dot dot-y';return'dot dot-g'}
function barColor(p){if(p===null)return'#22c55e';if(p<10)return'#ef4444';if(p<25)return'#eab308';return'#22c55e'}

// Health status dot class and label
function healthDotClass(status){
  if(status==='active')       return 'dot dot-g';
  if(status==='rate_limited') return 'dot dot-y';
  if(status==='error')        return 'dot dot-r';
  return 'dot dot-gray'; // idle or unknown
}
function healthLabel(status){
  if(status==='active')       return 'Active';
  if(status==='rate_limited') return 'Rate limited';
  if(status==='error')        return 'Error';
  return 'Idle';
}

let cTR,cTT,cH,cD;
const gctx=id=>document.getElementById(id).getContext('2d');
const bOpts={responsive:true,maintainAspectRatio:true,
  plugins:{legend:{labels:{color:'#8892a4',boxWidth:12,padding:10}}},
  scales:{x:{ticks:{color:'#8892a4'},grid:{color:'#2a2d3a'}},y:{ticks:{color:'#8892a4'},grid:{color:'#2a2d3a'},beginAtZero:true}}};
function initCharts(){
  cTR=new Chart(gctx('chart-today-req'),{type:'bar',data:{labels:[],datasets:[]},options:{...bOpts,plugins:{legend:{display:false}}}});
  cTT=new Chart(gctx('chart-today-tok'),{type:'bar',data:{labels:[],datasets:[]},options:{...bOpts,plugins:{legend:{display:false}}}});
  cH =new Chart(gctx('chart-hourly'),   {type:'line',data:{labels:[],datasets:[]},options:{...bOpts,elements:{line:{tension:.4},point:{radius:2}}}});
  cD =new Chart(gctx('chart-daily'),    {type:'bar',data:{labels:[],datasets:[]},options:{...bOpts}});
}
function toHr(ts){return new Date(ts*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}
function toDay(ts){return new Date(ts*1000).toLocaleDateString([],{month:'short',day:'numeric'})}

function update(data){
  document.getElementById('tot-req').textContent  =fmt(data.totals.requests);
  document.getElementById('tot-tok').textContent  =fmt(data.totals.tokens);
  document.getElementById('tot-err').textContent  =fmt(data.totals.errors);
  document.getElementById('tot-local').textContent=fmt(data.totals.local_fallbacks);
  document.getElementById('tot-req-sub').textContent='as of '+new Date(data.timestamp*1000).toLocaleTimeString();

  const grid=document.getElementById('provider-grid'); grid.innerHTML='';
  const pns=Object.keys(data.providers).sort();
  pns.forEach((name,ci)=>{
    const p=data.providers[name],rl=p.rate_limits||{};
    const health=(p.health||{status:'idle'});
    const rp=headPct(rl.rpm_used||0,rl.rpm_limit),rd=headPct(rl.rpd_used||0,rl.rpd_limit);
    const wp=[rp,rd].filter(x=>x!==null).reduce((a,b)=>Math.min(a,b),100);
    const hdc=healthDotClass(health.status);
    const col=COLORS[ci%COLORS.length];
    const c=document.createElement('div'); c.className='provider-card';
    c.innerHTML=`<div class="hdr"><span class="pname" style="color:${col}">${name}</span><span class="${hdc}" title="Health: ${healthLabel(health.status)}"></span></div>
      <div class="metric"><span>Status</span><span class="mval">${healthLabel(health.status)}</span></div>
      <div class="metric"><span>Requests today</span><span class="mval">${fmt(p.today.requests)}</span></div>
      <div class="metric"><span>Tokens today</span><span class="mval">${fmt(p.today.tokens)}</span></div>
      <div class="metric"><span>RPM headroom</span><span class="mval">${rp!==null?rp+'%':'no cap'}</span></div>
      <div class="metric"><span>RPD headroom</span><span class="mval">${rd!==null?rd+'%':'no cap'}</span></div>
      <div class="metric"><span>Errors today</span><span class="mval">${fmt(p.today.errors)}</span></div>
      <div class="pbar"><div class="pfill" style="width:${rp===null&&rd===null?100:wp||0}%;background:${barColor(rp===null&&rd===null?null:wp)}"></div></div>`;
    grid.appendChild(c);
  });

  const bc=pns.map((_,i)=>COLORS[i%COLORS.length]);
  cTR.data={labels:pns,datasets:[{data:pns.map(n=>data.providers[n].today.requests||0),backgroundColor:bc,borderRadius:4}]};cTR.update('none');
  cTT.data={labels:pns,datasets:[{data:pns.map(n=>data.providers[n].today.tokens||0),backgroundColor:bc,borderRadius:4}]};cTT.update('none');

  const hb={},hs=new Set();
  (data.hourly||[]).forEach(h=>{hs.add(h.provider);if(!hb[h.hour_ts])hb[h.hour_ts]={};hb[h.hour_ts][h.provider]=h.requests});
  const hts=Object.keys(hb).map(Number).sort(),hps=[...hs].sort();
  cH.data={labels:hts.map(toHr),datasets:hps.map((pn,ci)=>({label:pn,data:hts.map(t=>hb[t]?.[pn]||0),borderColor:COLORS[ci%COLORS.length],backgroundColor:COLORS[ci%COLORS.length]+'33',fill:false}))};cH.update('none');

  const db={},ds=new Set();
  (data.daily||[]).forEach(d=>{ds.add(d.provider);if(!db[d.day_ts])db[d.day_ts]={};db[d.day_ts][d.provider]=d.requests});
  const dts=Object.keys(db).map(Number).sort(),dps=[...ds].sort();
  cD.data={labels:dts.map(toDay),datasets:dps.map((pn,ci)=>({label:pn,data:dts.map(t=>db[t]?.[pn]||0),backgroundColor:COLORS[ci%COLORS.length],borderRadius:4,stack:'s'}))};
  cD.options.scales.x.stacked=cD.options.scales.y.stacked=true;cD.update('none');
}

// ============================================================
// Settings tab
// ============================================================
const _settingsHints={
  'disabled':  'Cloud APIs will be used for all requests. Your local Ollama model is only used as a fallback when all cloud quotas are exhausted.',
  'simple':    'Quick, one-line questions and short tasks will be handled by your local AI model. Cloud APIs are used for longer or more complex work.',
  'moderate':  'Most everyday requests go to your local AI model. Cloud APIs are reserved for complex agentic tasks with tools.',
  'always':    'All requests go to your local Ollama model — no cloud APIs are used. Useful if you want to work completely offline.',
};

async function loadSettings(){
  try{
    const r=await fetch('/api/settings');
    const d=await r.json();
    const sel=document.getElementById('settings-threshold');
    if(sel)sel.value=d.local_only_threshold||'simple';
    updateSettingsHint();
  }catch(e){console.warn('Could not load settings:',e);}
  loadLocalModel();
}

async function loadLocalModel(){
  try{
    const r=await fetch('/api/local-model');
    const d=await r.json();
    const sel=document.getElementById('local-model-select');
    if(sel)sel.value=d.current_model||'phi4-mini';
    const dot=document.getElementById('ollama-dot');
    const txt=document.getElementById('ollama-status-text');
    if(d.ollama_reachable){
      dot.className='dot dot-g';
      txt.textContent='Ollama is running on your machine';
      txt.style.color='var(--green)';
    }else{
      dot.className='dot dot-r';
      txt.textContent='Ollama not detected \u2014 install from ollama.com or ignore if not using local AI';
      txt.style.color='var(--muted)';
    }
  }catch(e){console.warn('Could not load local model:',e);}
}

async function saveLocalModel(){
  const btn=document.querySelector('#tab-settings .settings-card:nth-child(2) .btn-save');
  const msg=document.getElementById('local-model-save-msg');
  const val=document.getElementById('local-model-select').value;
  btn.disabled=true; msg.textContent='Saving\u2026'; msg.className='save-msg';
  try{
    const r=await fetch('/api/local-model',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model:val})});
    if(!r.ok)throw new Error(await r.text());
    msg.textContent=val==='none'?'Local fallback disabled.':'Saved! Pull the model if not already downloaded.';
    msg.className='save-msg ok';
    setTimeout(()=>{msg.textContent='';},4000);
    loadLocalModel();
  }catch(e){
    msg.textContent='Error: '+e.message;
    msg.className='save-msg err';
  }
  btn.disabled=false;
}

function updateSettingsHint(){
  const val=document.getElementById('settings-threshold').value;
  const hint=document.getElementById('settings-hint-text');
  if(hint)hint.textContent=_settingsHints[val]||'';
}

document.getElementById('settings-threshold').addEventListener('change',updateSettingsHint);

async function saveSettings(){
  const btn=document.querySelector('#tab-settings .btn-save');
  const msg=document.getElementById('settings-save-msg');
  const val=document.getElementById('settings-threshold').value;
  btn.disabled=true; msg.textContent='Saving…'; msg.className='save-msg';
  try{
    const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({local_only_threshold:val})});
    if(!r.ok)throw new Error(await r.text());
    msg.textContent='Saved!';
    msg.className='save-msg ok';
    setTimeout(()=>{msg.textContent='';},3000);
  }catch(e){
    msg.textContent='Error saving: '+e.message;
    msg.className='save-msg err';
  }
  btn.disabled=false;
}

let countdown=10;
async function fetchAndUpdate(){try{const r=await fetch('/api/dashboard-data');update(await r.json())}catch(e){console.warn(e)}}
function tick(){countdown--;document.getElementById('countdown').textContent=countdown;if(countdown<=0){countdown=10;fetchAndUpdate()}}
initCharts();fetchAndUpdate();setInterval(tick,1000);
</script>
</body>
</html>"""


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    return HTMLResponse(_DASHBOARD_HTML)
