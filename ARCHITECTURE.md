# FreeClawRouter — Architecture Reference

## 1. Overview

FreeClawRouter is a **standalone OpenAI-compatible reverse proxy**. It sits between OpenClaw (or any
OpenAI-compatible client) and the actual LLM inference endpoints. OpenClaw is never modified;
it simply has its `baseUrl` pointed at `http://localhost:8765/v1`.

```
┌──────────────────────────────────────────────────────────────────┐
│                         Docker Network                           │
│                                                                  │
│  ┌─────────┐    POST /v1/chat/completions    ┌────────────────┐  │
│  │         │ ───────────────────────────────▶│                │  │
│  │OpenClaw │                                 │   FreeClawRouter     │  │
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

FreeClawRouter exposes the same interface as the OpenAI API:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/models` | Lists all configured models; OpenClaw uses this for model discovery |
| `POST` | `/v1/chat/completions` | Main proxy endpoint; handles both streaming and non-streaming |
| `GET` | `/health` | Liveness probe used by Docker health checks |
| `GET` | `/stats` | Real-time rate-limit diagnostics |

The proxy is **model-transparent**: the `model` field in the outgoing request is
replaced with the upstream provider's actual model ID before forwarding. The client
always sees the virtual model ID (e.g. `freeclawrouter-auto`).

### Streaming (SSE)

OpenClaw sends `"stream": true` on every request. FreeClawRouter handles this with
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
│  Pre-Tier-1: local_only_threshold check             │
│                                                     │
│  If threshold == "always"                           │
│  OR threshold == "simple"  AND complexity == simple │
│  OR threshold == "moderate" AND complexity in       │
│                               (simple, moderate)    │
│    → return local fallback immediately              │
└──────────────────┬──────────────────────────────────┘
                   │ (threshold did not match)
                   ▼
┌─────────────────────────────────────────────────────┐
│  Tier 1: Hard Heuristic Filter                      │
│                                                     │
│  For each (provider, model) pair:                   │
│    ✗ Skip if in failed_providers set (retry loop)   │
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

### Ollama Router Think Mode

`gpt-oss:20b` is a reasoning model that uses string-based thinking levels
(`"low"`, `"medium"`, `"high"`) rather than boolean `true`/`false`. The router
call uses `"think": "low"` to minimise the reasoning trace so the visible
response (the option number) is generated within the `num_predict` token budget.
Other models that don't support this field ignore it silently.

---

## 4. Context Manager

OpenClaw's prompts can be large: the SOUL.md, AGENTS.md, TOOLS.md, and full
conversation history are concatenated into the system/user messages. This
can exceed the context window of free-tier models (some as low as 8K).

### Token Counting

`context_manager.py` uses `tiktoken` (cl100k_base encoding) as the primary
counter, with a `chars/4` approximation as a fallback. The `count_request_tokens`
function accounts for the messages array, tool schemas, and per-message overhead.

### Background Conversation Summarization

Rather than silently dropping old messages, FreeClawRouter proactively
summarizes them in the background using the local Ollama model.

```
After each successful request
        │
        ▼
maybe_schedule_summary()
  Is conversation > 60% of context window?
    No  → nothing to do
    Yes → split: [old turns] + [last 6 turns to keep]
          Is old_turns already cached?
            Yes → nothing to do
            No  → asyncio.create_task(_run_summary(old_turns))
                  ← returns immediately, never blocks request
                        │
                        ▼ (background, seconds later)
                  Ollama summarize call
                  Cache: message_hash → summary_text (max 256 entries)
```

The summary is injected on the **next** request that needs truncation:

```
fit_messages_to_context() called
        │
        ▼
  Step 0: summarizer.get_summary(old_msgs)
    Hit  → inject synthetic system message
           "[Earlier conversation — compressed]\n\n{summary}"
           + keep last 6 turns verbatim  →  usually fits, done
    Miss → fall through to drop strategy (same as before)
    (background task was already scheduled to warm the cache)
        │
        ▼
  Step 1: Drop oldest non-system messages (existing behaviour)
  Step 2: Truncate oldest remaining message content
  Step 3: Truncate system prompt (last resort, logged as warning)
```

Key properties:
- **Off the critical path**: the Ollama summarization call is always in a
  background task. Request latency is never affected.
- **Lossless when warm**: once the cache is primed (typically after the first
  long request), subsequent truncations replace dropped turns with a summary
  rather than discarding them silently.
- **Graceful degradation**: cache miss → falls back to the existing drop
  strategy transparently, no error surfaced to the client.

### Tuning Constants

| Constant | Default | Meaning |
|---|---|---|
| `SUMMARY_TRIGGER_PCT` | 0.60 | Schedule summarization when prompt > 60% of context |
| `RECENT_TURNS_TO_KEEP` | 6 | Always keep the last 6 non-system messages verbatim |
| `ConversationSummarizer._MAX_CACHE` | 256 | Max cached summaries in memory |

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
FreeClawRouter container
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

OpenClaw cannot access the host filesystem, the FreeClawRouter `.env` file, or any
other container's secrets.

---

## 8. Agentic Routing Constraints

OpenClaw uses multi-turn tool-calling loops where mid-stream model swaps would
corrupt the JSON tool-call response structure and cause parser crashes (PRD §2.3).

FreeClawRouter enforces this invariant with the following guarantee: **retries
only happen before the first data byte is sent to the client**. The
`_try_start_stream()` method in `proxy.py` probes the upstream connection and
checks the HTTP status before committing to stream — if the status is retryable
(429/5xx) it raises `_RetryableError` and the proxy selects a fresh provider
transparently. Once streaming has started (first `yield`), no mid-stream retry
is attempted; if an error occurs partway through a stream, an SSE error frame is
emitted and the stream is closed.

For non-streaming requests, the retry loop in `handle_chat_completions` retries
up to `len(providers) + 2` times, adding each failed provider to
`failed_providers` so the router can skip it on the next attempt.

---

## 9. Error Recovery / Interrupt Handling

```
Request arrives
      │
      ▼
 Route → provider A
      │
      ├── Success (2xx)  → record_success(A), return to client
      │
      ├── Retryable (429/5xx/404/timeout, BEFORE first byte sent)
      │     → record_rate_limit(A) or record_error(A)
      │     → add A to failed_providers
      │     → re-route (skip A) → provider B → …
      │
      ├── Local Ollama fallback (all cloud providers failed)
      │     → if Ollama unreachable → return 503 with clear message
      │     → if fallback_enabled=False → return 503 immediately
      │
      └── Non-retryable 4xx (e.g. 400 Bad Request)
            → return error to client immediately
```

Key properties:
- **Zero client visibility**: retries are fully transparent — the client
  never receives a partial response or an intermediate error frame.
- **Max attempts**: `len(configured_providers) + 2` (covers all cloud
  providers plus local fallback with one safety margin).
- **Local as last resort**: if every cloud provider fails, local Ollama
  is tried. If Ollama is unreachable, a clear 503 is returned immediately
  (no infinite retry loop).
- **Ollama optional**: FreeClawRouter operates without Ollama — cloud APIs
  handle everything. The router falls back to the Python recommendation if
  Ollama is unavailable. The dashboard Settings tab shows Ollama's status.
- **No mid-stream retry**: once bytes have been yielded to the client,
  no retry is possible (agentic constraint, see §8 above).

---

## 10. Provider Health Tracking

`health.py` maintains an in-memory status for each provider, updated
synchronously on every request outcome — zero additional API calls.

| Event | Status transition |
|---|---|
| `record_success(p)` | → `"active"` |
| `record_rate_limit(p)` | → `"rate_limited"` |
| `record_error(p, msg)` | consecutive_errors++; if ≥ 2 → `"error"` |
| No event in 10 minutes | → `"idle"` (evaluated lazily at read time) |

The `get_all()` method applies the idle-timeout rule at read time — there
is no background task or timer. The dashboard reads health state via the
`/api/dashboard-data` endpoint, which includes it in the `providers[name].health`
field.

---

## 11. File Structure

```
freeclawrouter/
├── src/
│   ├── __init__.py
│   ├── main.py             # FastAPI app, lifespan, routes, settings API
│   ├── config.py           # Config loading, data classes
│   ├── rate_limiter.py     # Sliding-window + day-counter buckets
│   ├── context_manager.py  # Token counting, message truncation
│   ├── router.py           # HybridRouter (local threshold + Tier 1 + Tier 2)
│   ├── proxy.py            # HTTP forwarding, retry loop, SSE streaming, OOM guard
│   ├── health.py           # Provider health tracker (zero extra API calls)
│   └── dashboard.py        # Web dashboard (Usage + Channels + Settings tabs)
├── config.yaml             # Default configuration (all providers)
├── config.local.yaml       # (git-ignored) Local overrides
├── .env                    # (git-ignored) API keys
├── .env.example            # Template for .env
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── README.md
├── ARCHITECTURE.md
└── INSTALL.md              # Step-by-step guide for non-technical users
```
