"""
FreeClawRouter – main.py
FastAPI application. Exposes an OpenAI-compatible API surface:

  POST /v1/chat/completions   — main proxy endpoint
  GET  /v1/models             — list all configured models
  GET  /health                — liveness probe
  GET  /stats                 — rate-limit diagnostics
  GET  /api/settings          — read runtime settings (local_only_threshold)
  POST /api/settings          — update runtime settings (persisted to JSON)
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
    threshold = _config.proxy.local_only_threshold if _config else "simple"
    return JSONResponse({
        "local_only_threshold": threshold,
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

    # Persist to JSON file so the setting survives a restart
    settings = _read_settings()
    if new_threshold is not None:
        settings["local_only_threshold"] = new_threshold
    _write_settings(settings)

    logger.info("Settings updated: local_only_threshold=%s", _config.proxy.local_only_threshold)
    return JSONResponse({"ok": True, "local_only_threshold": _config.proxy.local_only_threshold})


# ---------------------------------------------------------------------------
# POST /api/test-models  — connectivity test for all configured models
# ---------------------------------------------------------------------------

_TEST_PROMPT = [{"role": "user", "content": "Reply with exactly one word: OK"}]
_TEST_TIMEOUT = 20.0


async def _test_cloud_model(provider_name: str, base_url: str, api_key: str,
                             model_id: str, timeout: float) -> dict[str, Any]:
    url = f"{base_url}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"model": model_id, "messages": _TEST_PROMPT, "max_tokens": 10, "stream": False}
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
    except asyncio.TimeoutError:
        _health.record_error(provider_name, "timeout")
        _storage.record_request(provider_name, model_id, is_error=True)
        return {"provider": provider_name, "model": model_id, "ok": False,
                "latency_ms": int(timeout * 1000), "error": "timeout"}
    except Exception as exc:
        _health.record_error(provider_name, str(exc))
        _storage.record_request(provider_name, model_id, is_error=True)
        return {"provider": provider_name, "model": model_id, "ok": False,
                "latency_ms": int((time.monotonic() - start) * 1000), "error": str(exc)[:120]}


async def _test_local_model(base_url: str, model_id: str, timeout: float) -> dict[str, Any]:
    url = f"{base_url}/v1/chat/completions"
    payload = {"model": model_id, "messages": _TEST_PROMPT, "max_tokens": 10, "stream": False}
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
    except asyncio.TimeoutError:
        _storage.record_request("local", model_id, is_error=True, is_local=True)
        return {"provider": "local (Ollama)", "model": model_id, "ok": False,
                "latency_ms": int(timeout * 1000), "error": "timeout"}
    except Exception as exc:
        _storage.record_request("local", model_id, is_error=True, is_local=True)
        return {"provider": "local (Ollama)", "model": model_id, "ok": False,
                "latency_ms": int((time.monotonic() - start) * 1000), "error": str(exc)[:120]}


@app.post("/api/test-models")
async def test_models() -> JSONResponse:
    """
    Send a minimal one-shot request to every configured model in parallel
    and return pass/fail, latency, and a snippet of the response.
    Includes the local Ollama fallback model.
    """
    if _config is None:
        return JSONResponse(status_code=503, content={"error": "not initialised"})

    tasks: list[asyncio.Task] = []

    for provider in _config.providers:
        for model in provider.models:
            tasks.append(asyncio.create_task(
                _test_cloud_model(provider.name, provider.base_url, provider.api_key,
                                  model.id, _TEST_TIMEOUT)
            ))

    if _config.local.fallback_enabled:
        tasks.append(asyncio.create_task(
            _test_local_model(_config.local.base_url, _config.local.fallback_model, _TEST_TIMEOUT)
        ))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    clean = [
        r if isinstance(r, dict) else {"ok": False, "error": str(r)}
        for r in results
    ]
    passed = sum(1 for r in clean if r.get("ok"))
    return JSONResponse({"results": clean, "passed": passed, "total": len(clean)})


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
