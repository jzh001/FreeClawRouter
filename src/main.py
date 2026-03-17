"""
FreeClaw – main.py
FastAPI application. Exposes an OpenAI-compatible API surface:

  POST /v1/chat/completions   — main proxy endpoint
  GET  /v1/models             — list all configured models
  GET  /health                — liveness probe
  GET  /stats                 — rate-limit diagnostics
"""
from __future__ import annotations

import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .config import AppConfig, load_config
from .proxy import ForwardingProxy
from .rate_limiter import RateLimiterRegistry
from . import storage as _storage
from . import dashboard as _dashboard

# ---------------------------------------------------------------------------
# App state (populated at startup)
# ---------------------------------------------------------------------------

_config: AppConfig | None = None
_registry: RateLimiterRegistry | None = None
_proxy: ForwardingProxy | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _config, _registry, _proxy

    # Determine config directory (allow override via env for Docker)
    config_dir = Path(os.environ.get("FREECLAW_CONFIG_DIR", "."))

    _config = load_config(config_dir)

    # Configure logging
    log_level = getattr(logging, _config.proxy.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    logger = logging.getLogger("freeclaw")

    _storage.init_db()

    _registry = RateLimiterRegistry()
    for provider in _config.providers:
        _registry.register(provider)

    _dashboard.set_registry(_registry)

    _proxy = ForwardingProxy(_config, _registry)

    # Startup banner
    enabled = [p.name for p in _config.providers]
    border = "=" * 60
    logger.info(border)
    logger.info("  FreeClaw — Zero-Cost LLM Reverse Proxy")
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

    logger.info("FreeClaw shutting down.")


app = FastAPI(
    title="FreeClaw",
    description="OpenAI-compatible reverse proxy routing to free-tier LLM APIs",
    version="1.0.0",
    lifespan=lifespan,
)

logger = logging.getLogger("freeclaw")

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
        "id": "freeclaw-auto",
        "object": "model",
        "created": 0,
        "owned_by": "freeclaw",
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
# Entry point (for direct `python -m src.main` invocation)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    # Allow config dir override at startup
    config_dir = os.environ.get("FREECLAW_CONFIG_DIR", ".")
    cfg = load_config(config_dir)

    uvicorn.run(
        "src.main:app",
        host=cfg.proxy.host,
        port=cfg.proxy.port,
        log_level=cfg.proxy.log_level,
        reload=False,
    )
