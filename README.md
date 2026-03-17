# FreeClaw

**FreeClaw** is a zero-cost, self-hosted reverse proxy that lets you run **OpenClaw** (or any OpenAI-compatible agent) entirely on free-tier LLM APIs — with automatic failover to a local Ollama model when all cloud quotas are exhausted.

```
OpenClaw → FreeClaw (port 8765) → [ Cerebras / Groq / Gemini / OpenRouter / NVIDIA / SambaNova / Mistral ]
                                                   ↓ (all limits hit)
                                             Local Ollama (gpt-oss:20b)
```

---

## Requirements

| Dependency | Notes |
|---|---|
| [Docker Desktop](https://www.docker.com/products/docker-desktop/) | v24+ recommended |
| [Ollama](https://ollama.com) | Must be running on the host (or in Docker) |
| `gpt-oss:20b` model | `ollama pull gpt-oss:20b` |

---

## Quick Start

### 1. Pull the default local model

```bash
ollama pull gpt-oss:20b
```

### 2. Clone this repository

```bash
git clone https://github.com/your-username/freeclaw.git
cd freeclaw
```

### 3. Create your `.env` file with free-tier API keys

```bash
cp .env.example .env
```

Edit `.env` and fill in the keys for the providers you want to use.
You only need **one** — the more you add, the higher your combined free quota.

```ini
# .env
CEREBRAS_API_KEY=        # https://cloud.cerebras.ai  — 14,400 req/day free
GROQ_API_KEY=            # https://console.groq.com   — 14,400 req/day free
GOOGLE_AI_API_KEY=       # https://aistudio.google.com — 250 req/day free
OPENROUTER_API_KEY=      # https://openrouter.ai       — 200 req/day free
NVIDIA_API_KEY=          # https://build.nvidia.com    — 40 req/min free
SAMBANOVA_API_KEY=       # https://cloud.sambanova.ai  — 200K tokens/day free
MISTRAL_API_KEY=         # https://console.mistral.ai  — ~500K TPM free
```

Leave unused keys blank — FreeClaw automatically skips providers without a key.

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
      "freeclaw": {
        "baseUrl": "http://freeclaw:8765/v1",
        "apiKey": "freeclaw-local-key",
        "api": "openai-completions",
        "models": [
          {
            "id": "freeclaw-auto",
            "name": "FreeClaw Auto-Router",
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
      "models": ["freeclaw/freeclaw-auto"]
    }
  }
}
```

### 5. Launch the stack

```bash
docker compose up --build
```

FreeClaw is now listening on **http://localhost:8765**.

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

## Switching the Local Model

Change the `router_model` and `fallback_model` in `config.yaml` to any model you have pulled in Ollama:

```yaml
local:
  ollama:
    router_model: "qwen3.5:27b"    # Use a larger model for routing decisions
    fallback_model: "qwen3.5:27b"  # Use a larger model for generation
```

Then restart: `docker compose restart freeclaw`.

You can see all locally available models with `ollama list`.

---

## Watching Logs

```bash
# All containers
docker compose logs -f

# FreeClaw only (routing decisions, rate-limit warnings, OOM alerts)
docker compose logs -f freeclaw
```

When all free-tier API limits are exhausted, FreeClaw prints a prominent warning
in the logs before routing to local Ollama.

---

## Dashboard

FreeClaw includes a live web dashboard that shows API usage, token consumption,
provider health, and historical time-series charts.

**Access it at:** [http://localhost:8765/dashboard](http://localhost:8765/dashboard)

The dashboard auto-refreshes every 10 seconds and shows:

| Panel | What it shows |
|---|---|
| **Today's totals** | Total requests, tokens, errors, and local fallbacks since UTC midnight |
| **Provider status cards** | Per-provider: requests today, tokens today, live RPM/RPD headroom, colour-coded health dot |
| **Requests today (bar)** | Breakdown of today's requests by provider |
| **Tokens today (bar)** | Token consumption by provider |
| **Last 24 h (line)** | Hourly request volume per provider — see traffic patterns over time |
| **Last 7 days (bar)** | Daily request volume stacked by provider |

Usage data is stored in a persistent SQLite database (`data/freeclaw.db` inside
the Docker volume `freeclaw_data`). It survives container restarts and keeps a
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
| `GET /api/dashboard-data` | Dashboard data as JSON (for scripting/monitoring) |
| `POST /v1/chat/completions` | Main proxy endpoint (OpenAI-compatible) |
| `GET /v1/models` | List all configured models |
| `GET /health` | Liveness probe |
| `GET /stats` | Live rate-limit usage per provider (JSON) |

---

## Supported Free-Tier Providers

| Provider | Free RPD | Context | Sign-Up |
|---|---|---|---|
| Cerebras | 14,400 | 128K | [cloud.cerebras.ai](https://cloud.cerebras.ai) |
| Groq | 14,400 | 128K | [console.groq.com](https://console.groq.com) |
| Google AI Studio | 250 | 1M | [aistudio.google.com](https://aistudio.google.com) |
| OpenRouter | 200 | Varies | [openrouter.ai](https://openrouter.ai) |
| NVIDIA NIM | ~unlimited | 128K | [build.nvidia.com](https://build.nvidia.com) |
| SambaNova | 200K TPD | 8K–164K | [cloud.sambanova.ai](https://cloud.sambanova.ai) |
| Mistral (Experiment) | ~unlimited | 128K | [console.mistral.ai](https://console.mistral.ai) |

---

## Hardware Notes

- **Mac Mini M4 Pro / Apple Silicon**: Ollama uses Metal automatically — no extra config needed.
- **NVIDIA GPU**: Uncomment the `deploy.resources` section in `docker-compose.yml`.
- **CPU-only**: Works, but local fallback will be slower. Groq/Cerebras have the
  highest free daily quotas so local fallback should be rare.

---

## Troubleshooting

**FreeClaw says "no route" / returns 503**
All API keys may be exhausted for the day and `fallback_enabled` is `false` in
`config.yaml`. Either wait for daily limits to reset, add more API keys, or
set `fallback_enabled: true`.

**OpenClaw shows 4,096 token context window**
This is a known OpenClaw bug with custom providers. Ensure your `openclaw.json`
explicitly declares `"contextWindow": 131072` (or higher) in the model definition.

**Ollama router model not found**
Run `ollama pull gpt-oss:20b` on the host, or update `router_model` in
`config.yaml` to a model you have installed (`ollama list`).
