"""
FreeClawRouter – main.py
FastAPI application. Exposes an OpenAI-compatible API surface:

  POST /v1/chat/completions   — main proxy endpoint
  GET  /v1/models             — list all configured models
  GET  /health                — liveness probe
  GET  /stats                 — rate-limit diagnostics
  GET  /api/settings          — read runtime settings (local_only_threshold)
  POST /api/settings          — update runtime settings (persisted to JSON)
  GET  /api/local-model       — read local model config + Ollama status
  POST /api/local-model       — update local fallback model (persisted to JSON)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator

import httpx

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .config import AppConfig, load_config
from .proxy import ForwardingProxy
from .rate_limiter import RateLimiterRegistry
from . import storage as _storage
from . import dashboard as _dashboard
from .health import tracker as _health

# ---------------------------------------------------------------------------
# App state (populated at startup)
# ---------------------------------------------------------------------------

_config: AppConfig | None = None
_registry: RateLimiterRegistry | None = None
_proxy: ForwardingProxy | None = None

# Valid values for local_only_threshold
_VALID_THRESHOLDS = frozenset({"disabled", "simple", "moderate", "always"})

# Valid values for router_mode
_VALID_ROUTER_MODES = frozenset({"local", "python", "api"})

# Available local model choices (shown in the dashboard Settings tab).
# "none" disables local fallback entirely.
_LOCAL_MODELS = [
    "phi4-mini",
    "gpt-oss:20b",
    "qwen3.5:9b",
    "qwen3.5:4b",
    "qwen3.5:2b",
    "qwen3.5:0.8b",
    "none",
]


# ---------------------------------------------------------------------------
# Settings persistence helpers
# ---------------------------------------------------------------------------

def _settings_path() -> Path:
    data_dir = Path(os.environ.get("FREECLAWROUTER_DATA_DIR", "./data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "settings.json"


def _read_settings() -> dict[str, Any]:
    path = _settings_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _write_settings(data: dict[str, Any]) -> None:
    _settings_path().write_text(json.dumps(data, indent=2))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _config, _registry, _proxy

    # Determine config directory (allow override via env for Docker)
    config_dir = Path(os.environ.get("FREECLAWROUTER_CONFIG_DIR", "."))

    _config = load_config(config_dir)

    # Configure logging
    log_level = getattr(logging, _config.proxy.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    logger = logging.getLogger("freeclawrouter")

    _storage.init_db()

    _registry = RateLimiterRegistry()
    for provider in _config.providers:
        _registry.register(provider)

    _dashboard.set_registry(_registry)
    _dashboard.set_health_tracker(_health)

    # Apply any persisted runtime settings (e.g. local_only_threshold saved
    # via the dashboard Settings tab).  These override the config.yaml value
    # without requiring a restart.
    persisted = _read_settings()
    saved_threshold = persisted.get("local_only_threshold")
    if saved_threshold and saved_threshold in _VALID_THRESHOLDS:
        _config.proxy.local_only_threshold = saved_threshold
        logger.info("Applied persisted local_only_threshold: %s", saved_threshold)

    saved_router_mode = persisted.get("router_mode")
    if saved_router_mode and saved_router_mode in _VALID_ROUTER_MODES:
        _config.proxy.router_mode = saved_router_mode
        logger.info("Applied persisted router_mode: %s", saved_router_mode)

    saved_model = persisted.get("local_fallback_model")
    if saved_model and saved_model in _LOCAL_MODELS:
        if saved_model == "none":
            _config.local.fallback_enabled = False
        else:
            _config.local.fallback_model = saved_model
            _config.local.router_model = saved_model
            _config.local.fallback_enabled = True
        logger.info("Applied persisted local fallback model: %s", saved_model)

    _proxy = ForwardingProxy(_config, _registry)

    # Startup banner
    enabled = [p.name for p in _config.providers]
    border = "=" * 60
    logger.info(border)
    logger.info("  FreeClawRouter — Zero-Cost LLM Reverse Proxy")
    logger.info("  Listening on %s:%s", _config.proxy.host, _config.proxy.port)
    logger.info("  Active providers (%d): %s", len(enabled), ", ".join(enabled))
    logger.info(
        "  Local fallback: %s (%s)",
        _config.local.fallback_model,
        "enabled" if _config.local.fallback_enabled else "disabled",
    )
    if not enabled:
        logger.warning(
            "  ⚠  No API providers configured. All requests will use local Ollama."
        )
    logger.info(border)

    yield

    logger.info("FreeClawRouter shutting down.")


app = FastAPI(
    title="FreeClawRouter",
    description="OpenAI-compatible reverse proxy routing to free-tier LLM APIs",
    version="1.0.0",
    lifespan=lifespan,
)

logger = logging.getLogger("freeclawrouter")

app.include_router(_dashboard.router)


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "timestamp": time.time()}


# ---------------------------------------------------------------------------
# /stats
# ---------------------------------------------------------------------------

@app.get("/stats")
async def stats() -> dict:
    if _registry is None:
        return {"error": "not initialised"}
    return {
        "providers": _registry.all_stats(),
        "timestamp": time.time(),
    }


# ---------------------------------------------------------------------------
# GET /v1/models
# ---------------------------------------------------------------------------

@app.get("/v1/models")
async def list_models() -> dict:
    if _config is None:
        return JSONResponse(status_code=503, content={"error": "not initialised"})

    model_objects = []

    # Virtual "auto" model — routes to best available
    model_objects.append({
        "id": "freeclawrouter-auto",
        "object": "model",
        "created": 0,
        "owned_by": "freeclawrouter",
        "description": "Automatically routes to the best available free-tier model",
    })

    # Individual provider models exposed as provider/model-id
    for provider, model in _config.all_provider_models():
        model_objects.append({
            "id": f"{provider.name}/{model.id}",
            "object": "model",
            "created": 0,
            "owned_by": provider.name,
            "description": model.description,
            "context_window": model.context_window,
        })

    return {"object": "list", "data": model_objects}


# ---------------------------------------------------------------------------
# POST /v1/chat/completions
# ---------------------------------------------------------------------------

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    if _proxy is None:
        return JSONResponse(status_code=503, content={"error": "proxy not initialised"})

    try:
        payload: dict[str, Any] = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON body"})

    is_streaming = bool(payload.get("stream", False))

    status, headers, body = await _proxy.handle_chat_completions(payload)

    # AsyncIterator → StreamingResponse (SSE)
    if hasattr(body, "__aiter__"):
        return StreamingResponse(
            _consume_stream(body),
            status_code=status,
            headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
            media_type="text/event-stream",
        )

    return JSONResponse(status_code=status, content=body, headers=headers)


async def _consume_stream(gen) -> AsyncGenerator[bytes, None]:
    async for chunk in gen:
        yield chunk


# ---------------------------------------------------------------------------
# GET /api/settings  — read current runtime settings
# ---------------------------------------------------------------------------

@app.get("/api/settings")
async def get_settings() -> JSONResponse:
    if _config is None:
        return JSONResponse({"local_only_threshold": "disabled", "router_mode": "local"})
    return JSONResponse({
        "local_only_threshold": _config.proxy.local_only_threshold,
        "router_mode": _config.proxy.router_mode,
    })


# ---------------------------------------------------------------------------
# POST /api/settings  — update runtime settings (persisted across restarts)
# ---------------------------------------------------------------------------

@app.post("/api/settings")
async def post_settings(request: Request) -> JSONResponse:
    if _config is None:
        return JSONResponse(status_code=503, content={"error": "not initialised"})

    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON body"})

    new_threshold = body.get("local_only_threshold")
    if new_threshold is not None:
        if new_threshold not in _VALID_THRESHOLDS:
            return JSONResponse(
                status_code=400,
                content={"error": f"invalid local_only_threshold: {new_threshold!r}. "
                         f"Must be one of: {sorted(_VALID_THRESHOLDS)}"},
            )
        _config.proxy.local_only_threshold = new_threshold

    new_router_mode = body.get("router_mode")
    if new_router_mode is not None:
        if new_router_mode not in _VALID_ROUTER_MODES:
            return JSONResponse(
                status_code=400,
                content={"error": f"invalid router_mode: {new_router_mode!r}. "
                         f"Must be one of: {sorted(_VALID_ROUTER_MODES)}"},
            )
        _config.proxy.router_mode = new_router_mode

    # Persist to JSON file so settings survive a restart
    settings = _read_settings()
    if new_threshold is not None:
        settings["local_only_threshold"] = new_threshold
    if new_router_mode is not None:
        settings["router_mode"] = new_router_mode
    _write_settings(settings)

    logger.info(
        "Settings updated: local_only_threshold=%s router_mode=%s",
        _config.proxy.local_only_threshold, _config.proxy.router_mode,
    )
    return JSONResponse({
        "ok": True,
        "local_only_threshold": _config.proxy.local_only_threshold,
        "router_mode": _config.proxy.router_mode,
    })


# ---------------------------------------------------------------------------
# POST /api/clear-history  — delete usage history records
# ---------------------------------------------------------------------------

_CLEAR_PERIODS = {
    "hour":  3600,
    "day":   86400,
    "month": 30 * 86400,
    "all":   None,
}


@app.post("/api/clear-history")
async def clear_history(request: Request) -> JSONResponse:
    """
    Delete usage history for a given period.
    Body: {"period": "hour" | "day" | "month" | "all"}
    Deletes records older than now - period (or all records for "all").
    """
    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON body"})

    period = body.get("period")
    if period not in _CLEAR_PERIODS:
        return JSONResponse(
            status_code=400,
            content={"error": f"invalid period: {period!r}. Must be one of: {list(_CLEAR_PERIODS)}"},
        )

    seconds = _CLEAR_PERIODS[period]
    since_ts = None if seconds is None else int(time.time()) - seconds
    deleted = _storage.delete_history(since_ts)
    logger.info("Cleared history: period=%s, rows_deleted=%d", period, deleted)
    return JSONResponse({"ok": True, "period": period, "rows_deleted": deleted})


# ---------------------------------------------------------------------------
# GET /api/local-model  — read current local model + Ollama status
# ---------------------------------------------------------------------------

async def _check_ollama(base_url: str, timeout: float = 3.0) -> bool:
    """Return True if the Ollama server is reachable."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{base_url}/api/version")
            return resp.status_code == 200
    except Exception:
        return False


@app.get("/api/local-model")
async def get_local_model() -> JSONResponse:
    if _config is None:
        return JSONResponse(status_code=503, content={"error": "not initialised"})
    ollama_ok = await _check_ollama(_config.local.base_url)
    current = _config.local.fallback_model if _config.local.fallback_enabled else "none"
    return JSONResponse({
        "current_model": current,
        "available_models": _LOCAL_MODELS,
        "ollama_reachable": ollama_ok,
        "fallback_enabled": _config.local.fallback_enabled,
    })


# ---------------------------------------------------------------------------
# POST /api/local-model  — change local fallback model (persisted)
# ---------------------------------------------------------------------------

@app.post("/api/local-model")
async def post_local_model(request: Request) -> JSONResponse:
    if _config is None:
        return JSONResponse(status_code=503, content={"error": "not initialised"})

    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON body"})

    new_model = body.get("model")
    if new_model is None or new_model not in _LOCAL_MODELS:
        return JSONResponse(
            status_code=400,
            content={"error": f"invalid model: {new_model!r}. "
                     f"Must be one of: {_LOCAL_MODELS}"},
        )

    if new_model == "none":
        _config.local.fallback_enabled = False
    else:
        _config.local.fallback_model = new_model
        _config.local.router_model = new_model  # use same model for routing
        _config.local.fallback_enabled = True

    settings = _read_settings()
    settings["local_fallback_model"] = new_model
    _write_settings(settings)

    logger.info("Local model updated: %s (fallback_enabled=%s)",
                new_model, _config.local.fallback_enabled)
    return JSONResponse({
        "ok": True,
        "model": new_model,
        "fallback_enabled": _config.local.fallback_enabled,
    })


# ---------------------------------------------------------------------------
# POST /api/test-models  — connectivity test for all configured models
# ---------------------------------------------------------------------------

_TEST_PROMPT = [{"role": "user", "content": "Reply with exactly one word: OK"}]
_TEST_TIMEOUT = 20.0
_TEST_REASONING_TIMEOUT = 45.0  # reasoning models generate a think trace before answering
_LOCAL_TEST_TIMEOUT = 60.0      # local models may need time to load into GPU memory


def _is_timeout_error(exc: Exception) -> bool:
    """True for both asyncio.TimeoutError and httpx timeout exceptions."""
    return isinstance(exc, asyncio.TimeoutError) or isinstance(exc, httpx.TimeoutException)


async def _test_cloud_model(provider_name: str, base_url: str, api_key: str,
                             model_id: str, timeout: float,
                             is_reasoning: bool = False) -> dict[str, Any]:
    url = f"{base_url}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    # Reasoning models generate a hidden think trace that consumes tokens before the
    # visible answer — use a much larger budget so the output is never truncated.
    max_tokens = 512 if is_reasoning else 128
    payload = {"model": model_id, "messages": _TEST_PROMPT, "max_tokens": max_tokens, "stream": False}
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
        latency = int((time.monotonic() - start) * 1000)
        if resp.status_code == 200:
            data = resp.json()
            content = (data.get("choices") or [{}])[0].get("message") or {}
            content = (content.get("content") or "").strip()
            usage = data.get("usage") or {}
            tokens = usage.get("total_tokens") or usage.get("prompt_tokens", 0)
            # Record into rate-limiter, health tracker, and persistent storage
            if _registry:
                _registry.record_request(provider_name, tokens_used=tokens)
            _health.record_success(provider_name)
            _storage.record_request(provider_name, model_id,
                                    input_tokens=usage.get("prompt_tokens", 0),
                                    output_tokens=usage.get("completion_tokens", 0),
                                    total_tokens=tokens, duration_ms=latency)
            return {"provider": provider_name, "model": model_id, "ok": True,
                    "latency_ms": latency, "response": content[:80]}
        # Failed — record as error
        error_msg = f"HTTP {resp.status_code}"
        if resp.status_code == 429:
            if _registry:
                _registry.record_request(provider_name, tokens_used=0)
            _health.record_rate_limit(provider_name)
        else:
            _health.record_error(provider_name, error_msg)
        _storage.record_request(provider_name, model_id, is_error=True)
        return {"provider": provider_name, "model": model_id, "ok": False,
                "latency_ms": latency, "error": error_msg, "response": resp.text[:120]}
    except Exception as exc:
        elapsed = int((time.monotonic() - start) * 1000)
        if _is_timeout_error(exc):
            _health.record_error(provider_name, "timeout")
            _storage.record_request(provider_name, model_id, is_error=True)
            return {"provider": provider_name, "model": model_id, "ok": False,
                    "latency_ms": elapsed, "error": f"timeout (>{int(timeout)}s)"}
        _health.record_error(provider_name, str(exc))
        _storage.record_request(provider_name, model_id, is_error=True)
        return {"provider": provider_name, "model": model_id, "ok": False,
                "latency_ms": elapsed, "error": str(exc)[:120] or repr(exc)[:120]}


async def _test_local_model(base_url: str, model_id: str, timeout: float) -> dict[str, Any]:
    url = f"{base_url}/v1/chat/completions"
    payload = {"model": model_id, "messages": _TEST_PROMPT, "max_tokens": 32, "stream": False}
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers={"Authorization": "Bearer ollama",
                                                   "Content-Type": "application/json"},
                                     json=payload)
        latency = int((time.monotonic() - start) * 1000)
        if resp.status_code == 200:
            data = resp.json()
            content = (data.get("choices") or [{}])[0].get("message") or {}
            content = (content.get("content") or "").strip()
            usage = data.get("usage") or {}
            tokens = usage.get("total_tokens") or usage.get("prompt_tokens", 0)
            _storage.record_request("local", model_id,
                                    input_tokens=usage.get("prompt_tokens", 0),
                                    output_tokens=usage.get("completion_tokens", 0),
                                    total_tokens=tokens, duration_ms=latency, is_local=True)
            return {"provider": "local (Ollama)", "model": model_id, "ok": True,
                    "latency_ms": latency, "response": content[:80]}
        _storage.record_request("local", model_id, is_error=True, is_local=True)
        return {"provider": "local (Ollama)", "model": model_id, "ok": False,
                "latency_ms": latency, "error": f"HTTP {resp.status_code}",
                "response": resp.text[:120]}
    except Exception as exc:
        elapsed = int((time.monotonic() - start) * 1000)
        _storage.record_request("local", model_id, is_error=True, is_local=True)
        if _is_timeout_error(exc):
            return {"provider": "local (Ollama)", "model": model_id, "ok": False,
                    "latency_ms": elapsed, "error": f"timeout (>{int(timeout)}s)"}
        return {"provider": "local (Ollama)", "model": model_id, "ok": False,
                "latency_ms": elapsed, "error": str(exc)[:120] or repr(exc)[:120]}


@app.post("/api/test-models")
async def test_models() -> JSONResponse:
    """
    Send a minimal one-shot request to every configured cloud model in parallel
    and return pass/fail, latency, and a snippet of the response.
    Local Ollama is tested separately via /api/test-local (it may need time to load).
    """
    if _config is None:
        return JSONResponse(status_code=503, content={"error": "not initialised"})

    tasks: list[asyncio.Task] = []

    for provider in _config.providers:
        for model in provider.models:
            timeout = _TEST_REASONING_TIMEOUT if model.is_reasoning else _TEST_TIMEOUT
            tasks.append(asyncio.create_task(
                _test_cloud_model(provider.name, provider.base_url, provider.api_key,
                                  model.id, timeout, is_reasoning=model.is_reasoning)
            ))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    clean = [
        r if isinstance(r, dict) else {"ok": False, "error": str(r)}
        for r in results
    ]
    passed = sum(1 for r in clean if r.get("ok"))
    return JSONResponse({"results": clean, "passed": passed, "total": len(clean)})


@app.post("/api/test-local")
async def test_local() -> JSONResponse:
    """
    Test the configured local Ollama model with a generous 60-second timeout.
    Local models may need time to load into GPU memory on first call.
    """
    if _config is None:
        return JSONResponse(status_code=503, content={"error": "not initialised"})

    if not _config.local.fallback_enabled or _config.local.fallback_model == "none":
        return JSONResponse({"results": [], "passed": 0, "total": 0,
                             "note": "Local fallback is disabled."})

    result = await _test_local_model(
        _config.local.base_url, _config.local.fallback_model, _LOCAL_TEST_TIMEOUT
    )
    passed = 1 if result.get("ok") else 0
    return JSONResponse({"results": [result], "passed": passed, "total": 1})


# ---------------------------------------------------------------------------
# Entry point (for direct `python -m src.main` invocation)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    # Allow config dir override at startup
    config_dir = os.environ.get("FREECLAWROUTER_CONFIG_DIR", ".")
    cfg = load_config(config_dir)

    uvicorn.run(
        "src.main:app",
        host=cfg.proxy.host,
        port=cfg.proxy.port,
        log_level=cfg.proxy.log_level,
        reload=False,
    )
