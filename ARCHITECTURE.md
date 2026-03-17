# FreeClaw — Architecture Reference

## 1. Overview

FreeClaw is a **standalone OpenAI-compatible reverse proxy**. It sits between OpenClaw (or any
OpenAI-compatible client) and the actual LLM inference endpoints. OpenClaw is never modified;
it simply has its `baseUrl` pointed at `http://localhost:8765/v1`.

```
┌──────────────────────────────────────────────────────────────────┐
│                         Docker Network                           │
│                                                                  │
│  ┌─────────┐    POST /v1/chat/completions    ┌────────────────┐  │
│  │         │ ───────────────────────────────▶│                │  │
│  │OpenClaw │                                 │   FreeClaw     │  │
│  │ Agent   │ ◀───────────────────────────────│   Proxy        │  │
│  │         │      SSE stream / JSON           │   :8765        │  │
│  └─────────┘                                 └───────┬────────┘  │
│                                                      │           │
│                              ┌───────────────────────┤           │
│                              ▼                       ▼           │
│                    ┌──────────────────┐   ┌──────────────────┐  │
│                    │  Cloud Free APIs │   │  Ollama (local)  │  │
│                    │  Cerebras/Groq/  │   │  gpt-oss:20b     │  │
│                    │  Gemini/etc.     │   │  (router+fallback)│  │
│                    └──────────────────┘   └──────────────────┘  │
└──────────────────────────────────────────────────────────────────┘
```

---

## 2. Reverse Proxy Abstraction

FreeClaw exposes the same interface as the OpenAI API:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/models` | Lists all configured models; OpenClaw uses this for model discovery |
| `POST` | `/v1/chat/completions` | Main proxy endpoint; handles both streaming and non-streaming |
| `GET` | `/health` | Liveness probe used by Docker health checks |
| `GET` | `/stats` | Real-time rate-limit diagnostics |

The proxy is **model-transparent**: the `model` field in the outgoing request is
replaced with the upstream provider's actual model ID before forwarding. The client
always sees the virtual model ID (e.g. `freeclaw-auto`).

### Streaming (SSE)

OpenClaw sends `"stream": true` on every request. FreeClaw handles this with
`httpx.AsyncClient.stream()` and forwards raw SSE bytes chunk-by-chunk to the
client via FastAPI's `StreamingResponse`. There is no buffering of the full
response — latency from the upstream is preserved.

---

## 3. Hybrid Router — Decision Pipeline

```
Incoming request
      │
      ▼
┌─────────────────────────────────────────────────────┐
│  Tier 1: Hard Heuristic Filter                      │
│                                                     │
│  For each (provider, model) pair:                   │
│    ✗ Skip if API key not configured                 │
│    ✗ Skip if context_window < input_tokens + 4096   │
│    ✗ Skip if rate limits exhausted (RPM/RPD/TPM/TPD)│
│    ✓ Build CandidateInfo with detailed stats        │
└──────────────────┬──────────────────────────────────┘
                   │
         0 candidates?    1 candidate?    2+ candidates?
               │                │               │
               ▼                ▼               ▼
        ┌──────────┐     ┌──────────┐   ┌─────────────────────────┐
        │  Local   │     │  Use it  │   │  Python Scheduling Score │
        │ Fallback │     │  (done)  │   │  _score_candidate()      │
        │ (warn)   │     └──────────┘   └──────────┬──────────────┘
        └──────────┘                               │
                                                   ▼
                                       ┌─────────────────────────┐
                                       │  Ollama LLM Confirmation │
                                       │  (structured prompt with │
                                       │   rules + recommendation)│
                                       └──────────┬──────────────┘
                                                  │
                                                  ▼
                                         ┌────────────────┐
                                         │ Chosen provider│
                                         │  + model       │
                                         └────────────────┘
```

### Tier 2: Scheduling Score + LLM Confirmation

The Python scheduler pre-computes a deterministic 0–100 score for each
candidate using four OS-scheduling-inspired heuristics:

| Heuristic | Analogy | Weight |
|---|---|---|
| **Daily budget preservation** | Non-renewable resource (disk writes) | ±40 pts |
| **Fast-refresh preference** | CPU time (renewable) over DRAM (scarce) | +20 pts |
| **Fairness / Weighted Round-Robin** | Deficit Round-Robin scheduling | +15 pts |
| **Complexity matching** | Memory tiering (right-size the model) | ±10 pts |
| **Provider priority** | Quality tiebreaker | +5 pts |

The highest-scoring candidate becomes the **Python recommendation**. The
local Ollama model is then given a structured, rule-annotated prompt that:
1. Explains the scheduling rules explicitly (the LLM must apply them, not
   invent them — critical for small models like gpt-oss:20b)
2. Shows all candidates with their scores, headrooms, and status badges
3. States the recommendation and asks the LLM to confirm or override

If the Ollama call fails or times out, the proxy falls back to the Python
recommendation transparently — no request is ever blocked.

### Scheduling Rules (encoded in the prompt)

```
RULE 1 — HARD BLOCK:    Never choose [LOW] budget (<10% daily headroom)
RULE 2 — FAST-REFRESH:  Prefer [FAST-REFRESH] (RPM, resets in <60s)
                        over [DAILY BUDGET] (RPD, resets at midnight)
RULE 3 — FAIRNESS:      Among similar-scoring options, pick the one with
                        the most remaining daily headroom (round-robin)
RULE 4 — COMPLEXITY:    complex → need ≥128K context
                        simple  → avoid oversized models (save slots)
RULE 5 — TIEBREAKER:    Lower provider priority number wins
```

### Routing Prompt Security

The prompt contains **only scheduling metadata** — no API keys, no user
message content, no authentication data. This satisfies PRD §4.1.

---

## 4. Context Manager

OpenClaw's prompts can be large: the SOUL.md, AGENTS.md, TOOLS.md, and full
conversation history are concatenated into the system/user messages. This
can exceed the context window of free-tier models (some as low as 8K).

### Token Counting

`context_manager.py` uses `tiktoken` (cl100k_base encoding) as the primary
counter, with a `chars/4` approximation as a fallback. The `count_request_tokens`
function accounts for the messages array, tool schemas, and per-message overhead.

### Truncation Strategy (in order of preference)

```
1. Drop oldest non-system messages from the front of the conversation
   (preserves system prompt + recent turns)

2. If still over limit: truncate the content of the oldest remaining
   non-system message

3. Last resort: truncate the system prompt itself (logged as a warning)
```

The effective context limit is `context_window - output_reserve` (default: 4096
tokens reserved for the model's output).

---

## 5. Rate Limiter

`rate_limiter.py` maintains per-provider in-memory buckets with:

| Window | Metric | Implementation |
|---|---|---|
| Rolling 60s | RPM | `collections.deque` with monotonic timestamps |
| Rolling 60s | TPM | Same, accumulating token counts |
| Calendar day (UTC) | RPD | Counter that resets at midnight UTC |
| Calendar day (UTC) | TPD | Same, accumulating token counts |

Rate limit data is ephemeral (resets on restart). This is intentional: free-tier
API providers track quotas server-side. The in-memory tracker is a best-effort
client-side guard to avoid unnecessary 429 responses.

When an upstream 429 is received, the bucket is immediately marked as saturated
(by recording a request), so subsequent requests skip that provider.

---

## 6. OOM Safeguard

`psutil` monitors host memory during local Ollama fallback calls:

| Threshold | Action |
|---|---|
| `memory_warning_threshold` (80%) | Log warning; double the output reserve to force more aggressive context truncation |
| `memory_critical_threshold` (90%) | Abort the request with HTTP 503; log error |

Both thresholds are configurable in `config.yaml`.

---

## 7. Security Architecture

### API Key Isolation

```
.env (host only)
    │
    ▼ (docker env injection)
FreeClaw container
    │
    ├── used in Authorization header when forwarding to cloud APIs
    └── NEVER included in:
          • routing prompts sent to Ollama
          • responses sent back to OpenClaw
          • /stats endpoint
```

### OpenClaw Sandbox

The `openclaw` service in `docker-compose.yml` enforces:
- `read_only: true` — root filesystem is read-only
- `tmpfs` only for `/tmp` and `/run` — no persistent writes to host
- No bind mounts to host directories beyond `./openclaw_config`
- `cap_drop: ALL` — all Linux capabilities dropped
- `no-new-privileges: true` — no privilege escalation

OpenClaw cannot access the host filesystem, the FreeClaw `.env` file, or any
other container's secrets.

---

## 8. Agentic Routing Constraints

OpenClaw uses multi-turn tool-calling loops where mid-stream model swaps would
corrupt the JSON tool-call response structure and cause parser crashes (PRD §2.3).

FreeClaw enforces this invariant by making **exactly one routing decision per
incoming HTTP request**. The `RouteDecision` object is committed before the
upstream request begins and is never changed mid-stream. If an upstream returns
an error mid-stream, FreeClaw emits an SSE error frame and closes the stream;
it does **not** attempt to restart the generation on a different provider.

---

## 9. File Structure

```
freeclaw/
├── src/
│   ├── __init__.py
│   ├── main.py             # FastAPI app, lifespan, routes
│   ├── config.py           # Config loading, data classes
│   ├── rate_limiter.py     # Sliding-window + day-counter buckets
│   ├── context_manager.py  # Token counting, message truncation
│   ├── router.py           # HybridRouter (Tier 1 + Tier 2)
│   └── proxy.py            # HTTP forwarding, SSE streaming, OOM guard
├── config.yaml             # Default configuration (all providers)
├── config.local.yaml       # (git-ignored) Local overrides
├── .env                    # (git-ignored) API keys
├── .env.example            # Template for .env
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── README.md
└── ARCHITECTURE.md
```
