# FreeClawRouter

**FreeClawRouter** is a self-hosted OpenAI-compatible reverse proxy that intelligently distributes requests across multiple LLM providers — balancing rate limits, latency, and task complexity. When all configured cloud providers are unavailable, it falls back to a local Ollama model.

> Each provider has its own usage limits and pricing. FreeClawRouter works with whichever providers you have configured — see each provider's terms of service for details on permitted use.

```
OpenClaw → FreeClawRouter (port 8765) → [ Cerebras / Groq / Gemini / OpenRouter / NVIDIA / SambaNova / Mistral ]
                                                   ↓ (all providers unavailable)
                                             Local Ollama (phi4-mini)
```

---

## Features

- **Smart routing** — hybrid two-tier engine combines deterministic scheduling scores with local LLM confirmation to pick the best provider for each request
- **Error rerouting** — on a retryable failure (429, 5xx, connection error) the failed provider is automatically skipped and the next best option is tried, transparently, before any bytes reach the client
- **Local-first threshold** — configurable `local_only_threshold` option routes simple or all tasks directly to your local Ollama model; adjustable without a restart via the dashboard Settings tab
- **Provider health tracking** — each provider's status (active / rate limited / error / idle) is derived from request outcomes in real time — zero extra API calls — and shown as colour-coded dots on the dashboard
- **Background conversation summarization** — long conversations are automatically summarized by the local Ollama model in the background (off the critical path) so old turns are compressed rather than silently dropped when the context window fills
- **Multi-provider orchestration** — routes across Cerebras, Groq, Gemini, OpenRouter, NVIDIA NIM, SambaNova, and Mistral, with local Ollama as the final fallback; each provider's rate limits are tracked independently

---

## Requirements

| Dependency | Notes |
|---|---|
| [Docker Desktop](https://www.docker.com/products/docker-desktop/) | v24+ recommended |
| At least one API key | Cerebras, Groq, Gemini, OpenRouter, NVIDIA, SambaNova, or Mistral |
| [Ollama](https://ollama.com) | **Optional but recommended** — enables local fallback and smarter routing |

---

## Quick Start

### 1. (Optional) Install Ollama for local fallback

Ollama is not required — cloud APIs work without it. However, it unlocks smarter
routing decisions and allows the proxy to keep working when all cloud quotas are
exhausted.

```bash
# Install from https://ollama.com, then pull the default router model:
ollama pull phi4-mini
```

You can choose a different local model from the dashboard **Settings** tab after
startup (`phi4-mini`, `gpt-oss:20b`, `qwen3.5:9b/4b/2b/0.8b`, or disable local fallback).

### 2. Clone this repository

```bash
git clone https://github.com/your-username/freeclawrouter.git
cd freeclawrouter
```

### 3. Create your `.env` file with API keys

```bash
cp .env.example .env
```

Edit `.env` and fill in the keys for the providers you want to use.
You only need **one** — the more you add, the more capacity FreeClawRouter can balance across.

```ini
# .env
CEREBRAS_API_KEY=        # https://cloud.cerebras.ai
GROQ_API_KEY=            # https://console.groq.com
GOOGLE_AI_API_KEY=       # https://aistudio.google.com
OPENROUTER_API_KEY=      # https://openrouter.ai
NVIDIA_API_KEY=          # https://build.nvidia.com
SAMBANOVA_API_KEY=       # https://cloud.sambanova.ai
MISTRAL_API_KEY=         # https://console.mistral.ai
```

Leave unused keys blank — FreeClawRouter automatically skips providers without a key.
Each provider's usage is subject to their own terms of service and rate limits.

### 4. (Optional) Configure OpenClaw

Create the OpenClaw config directory and a minimal provider config:

```bash
mkdir -p openclaw_config
```

Create `openclaw_config/openclaw.json` (JSON5 format):

```jsonc
{
  "models": {
    "providers": {
      "freeclawrouter": {
        "baseUrl": "http://freeclawrouter:8765/v1",
        "apiKey": "freeclawrouter-local-key",
        "api": "openai-completions",
        "models": [
          {
            "id": "freeclawrouter-auto",
            "name": "FreeClawRouter Auto-Router",
            "contextWindow": 131072,
            "maxTokens": 4096,
            "cost": { "input": 0, "output": 0 }
          }
        ]
      }
    }
  },
  "agents": {
    "defaults": {
      "models": {
        "freeclawrouter/freeclawrouter-auto": {}
      }
    }
  }
}
```

### 5. Launch the stack

```bash
docker compose up --build
```

FreeClawRouter is now listening on **http://localhost:8765**.

To run in the background:

```bash
docker compose up -d --build
```

### 6. Verify

```bash
curl http://localhost:8765/health
# → {"status":"ok","timestamp":...}

curl http://localhost:8765/v1/models
# → {"object":"list","data":[...]}

curl http://localhost:8765/stats
# → per-provider rate-limit usage
```

---

## Chatting with OpenClaw via Messaging Apps

OpenClaw has a built-in gateway that connects it to 25+ messaging apps including
WhatsApp and Telegram. When running via Docker Compose, the OpenClaw container
already has the gateway built in.

**How the config works:**
The file `openclaw_config/openclaw.json` in this directory is bind-mounted into
the OpenClaw container at `/home/node/.openclaw/openclaw.json`. You can edit it
directly on the host — no need to enter the container. Alternatively, use the
**[Channels tab](http://localhost:8765/dashboard)** in the FreeClawRouter dashboard
for a guided UI.

After editing the config, restart OpenClaw to pick up changes:

```bash
docker compose restart openclaw
```

---

### Option A — Dashboard (recommended)

Open the **Channels** tab at [http://localhost:8765/dashboard](http://localhost:8765/dashboard).
Fill in your bot token and access policy, click **Save**, then follow the
pairing instructions shown on the page.

---

### Option B — Manual config

Edit `openclaw_config/openclaw.json` directly on the host:

```jsonc
{
  // ... existing models/agents config ...
  "channels": {
    "telegram": {
      "enabled": true,
      "botToken": "123456789:ABCdef...",   // from @BotFather
      "dmPolicy": "pairing",               // pairing | allowlist | open | disabled
      "allowFrom": [],                     // numeric Telegram user IDs (allowlist mode)
      "groupPolicy": "disabled"
    },
    "whatsapp": {
      "dmPolicy": "pairing",               // pairing | allowlist | open | disabled
      "allowFrom": [],                     // E.164 phone numbers (allowlist mode)
      "groupPolicy": "disabled"
    }
  }
}
```

Then restart: `docker compose restart openclaw`

---

### Telegram setup

**1. Create a bot** — send `/newbot` to [@BotFather](https://t.me/BotFather) and
copy the token it gives you.

**2. Configure** — add the token via the dashboard Channels tab, or edit
`openclaw_config/openclaw.json` manually (see above). Restart OpenClaw.

**3. Pair** — send any message to your bot. It replies with an 8-character pairing
code. Approve it:

```bash
docker exec freeclawrouter_openclaw openclaw pairing list telegram
docker exec freeclawrouter_openclaw openclaw pairing approve telegram <CODE>
```

**Lock down to yourself** — find your numeric user ID via [@userinfobot](https://t.me/userinfobot),
set `"dmPolicy": "allowlist"` and add it to `"allowFrom"`.

---

### WhatsApp setup

WhatsApp uses the Baileys (WhatsApp Web) protocol. Use a **dedicated phone number**
rather than your primary WhatsApp.

**1. Scan QR code** — run this and scan the QR code from WhatsApp on your phone
(**Linked Devices → Link a Device**):

```bash
docker exec -it freeclawrouter_openclaw openclaw channels login --channel whatsapp
```

**2. Configure** — set your access policy in `openclaw_config/openclaw.json` (or
via the dashboard). Restart OpenClaw.

**3. Pair** — send any message from your phone to the linked number. Approve the
pairing code:

```bash
docker exec freeclawrouter_openclaw openclaw pairing list whatsapp
docker exec freeclawrouter_openclaw openclaw pairing approve whatsapp <CODE>
```

**Lock down to yourself** — set `"dmPolicy": "allowlist"` and add your phone number
in E.164 format (e.g. `"+15551234567"`) to `"allowFrom"`.

---

## Switching the Local Model

The easiest way is via the dashboard **Settings** tab — select from the dropdown and click Save. No restart needed.

Alternatively, edit `config.yaml` directly:

```yaml
local:
  ollama:
    router_model: "gpt-oss:20b"    # used for routing decisions — must be fast
    fallback_model: "gpt-oss:20b"  # used for actual inference fallback
    fallback_enabled: true          # set to false to disable local fallback
```

Then restart: `docker compose restart freeclawrouter`. See `ollama list` for installed models.

## Web Search in OpenClaw

OpenClaw supports web search via multiple providers. Add your `GOOGLE_AI_API_KEY` to
`.env` (you likely have this already for Gemini) — FreeClawRouter passes it to the
OpenClaw container automatically. OpenClaw will use Gemini for web search grounding.

Other search providers (Brave, Perplexity, Grok) are also supported if you have those
API keys. Add them to `.env` and the OpenClaw container's environment in `docker-compose.yml`.

---

## Watching Logs

```bash
# All containers
docker compose logs -f

# FreeClawRouter only (routing decisions, rate-limit warnings, OOM alerts)
docker compose logs -f freeclawrouter
```

When all free-tier API limits are exhausted, FreeClawRouter prints a prominent warning
in the logs before routing to local Ollama.

---

## Dashboard

FreeClawRouter includes a live web dashboard that shows API usage, token consumption,
provider health, and historical time-series charts.

**Access it at:** [http://localhost:8765/dashboard](http://localhost:8765/dashboard)

The dashboard auto-refreshes every 10 seconds and shows:

| Panel | What it shows |
|---|---|
| **Today's totals** | Total requests, tokens, errors, and local fallbacks since UTC midnight |
| **Provider status cards** | Per-provider: real-time health status dot (active/rate-limited/error/idle), requests, tokens, RPM/RPD headroom |
| **Requests today (bar)** | Breakdown of today's requests by provider |
| **Tokens today (bar)** | Token consumption by provider |
| **Last 24 h (line)** | Hourly request volume per provider — see traffic patterns over time |
| **Last 7 days (bar)** | Daily request volume stacked by provider |
| **Test Models tab** | Cloud models: one-shot test in parallel (20 s each). Local model: separate button with 60 s timeout for cold-start loading |
| **Chat tab** | ChatGPT-like interface that streams responses directly from any configured provider. Supports model selection, multi-turn memory, and markdown rendering |
| **Logs tab** | Real-time terminal stream of OpenClaw container stdout/stderr via the Docker socket API. Connect/Disconnect, Clear, auto-scroll toggle, line counter, colour-coded by log level |
| **Settings → Router mode** | `local` (Ollama LLM, default) / `python` (deterministic scoring, fastest) / `api` (fast cloud model) — no restart needed |
| **Settings → Local AI fallback** | When to route to local Ollama instead of cloud APIs (disabled / simple / moderate / always) |
| **Settings → Local Model** | Ollama connection status indicator + dropdown to select which local model to use; supports `phi4-mini`, `gpt-oss:20b`, `qwen3.5` variants, or "none" to disable |
| **Usage → Clear History** | Delete usage records by time period (past hour / day / month / all) |

Usage data is stored in a persistent SQLite database (`data/freeclawrouter.db` inside
the Docker volume `freeclawrouter_data`). It survives container restarts and keeps a
full history for trend analysis.

To access the dashboard from a remote machine or another container:
```
http://<host-ip>:8765/dashboard
```

---

## Endpoints

| Endpoint | Description |
|---|---|
| `GET /dashboard` | Live usage dashboard (web UI) |
| `GET /api/dashboard-data` | Dashboard data as JSON (providers, health, charts) |
| `GET /api/settings` | Read current runtime settings (e.g. local_only_threshold) |
| `POST /api/settings` | Update runtime settings (persisted, no restart needed) |
| `GET /api/local-model` | Read local model config and Ollama reachability status |
| `POST /api/local-model` | Change local fallback model (persisted, no restart needed) |
| `POST /api/test-models` | Test all configured cloud models in parallel; returns pass/fail + latency |
| `POST /api/test-local` | Test the local Ollama model with a 60 s timeout (handles cold-start loading) |
| `POST /api/clear-history` | Delete usage records by period (`{"period":"hour"\|"day"\|"month"\|"all"}`) |
| `GET /api/openclaw-logs` | SSE stream of OpenClaw container logs (stdout + stderr) via Docker socket |
| `POST /v1/chat/completions` | Main proxy endpoint (OpenAI-compatible) |
| `GET /v1/models` | List all configured models |
| `GET /health` | Liveness probe |
| `GET /stats` | Live rate-limit usage per provider (JSON) |

---

## Supported Providers

| Provider | Rate Limits | Max Context | Sign-Up |
|---|---|---|---|
| Cerebras | 30 RPM / 14,400 RPD | 128K | [cloud.cerebras.ai](https://cloud.cerebras.ai) |
| Groq | 30 RPM / 1,000–14,400 RPD | 128K | [console.groq.com](https://console.groq.com) |
| Google AI Studio | 10 RPM / 250 RPD | 1M | [aistudio.google.com](https://aistudio.google.com) |
| OpenRouter | 20 RPM / 200 RPD | Varies | [openrouter.ai](https://openrouter.ai) |
| NVIDIA NIM | 40 RPM / credit-based | 262K | [build.nvidia.com](https://build.nvidia.com) |
| SambaNova | 20 RPM / 20 RPD | 164K | [cloud.sambanova.ai](https://cloud.sambanova.ai) |
| Mistral | 2 RPM | 256K | [console.mistral.ai](https://console.mistral.ai) |

> Limits shown are approximate and tier-dependent. Check each provider's current pricing and terms before use.

---

## Hardware Notes

- **M4 Apple Silicon**: Ollama uses Metal automatically — no extra config needed.
- **NVIDIA GPU**: Uncomment the `deploy.resources` section in `docker-compose.yml`.
- **CPU-only**: Works, but local fallback will be slower. Groq/Cerebras have the
  highest free daily quotas so local fallback should be rare.

---

## Troubleshooting

**FreeClawRouter says "no route" / returns 503**
All API keys may be exhausted for the day and `fallback_enabled` is `false` in
`config.yaml`. Either wait for daily limits to reset, add more API keys, or
set `fallback_enabled: true`.

**OpenClaw shows 4,096 token context window**
This is a known OpenClaw bug with custom providers. Ensure your `openclaw.json`
explicitly declares `"contextWindow": 131072` (or higher) in the model definition.

**Ollama router model not found**
Run `ollama pull phi4-mini` on the host (default), or select a different model
in the dashboard Settings tab. Run `ollama list` to see installed models.
