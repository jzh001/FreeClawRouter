"""
FreeClaw – proxy.py
HTTP forwarding layer with full SSE streaming support.

Responsibilities:
  1. Build the upstream request (correct URL, auth header, adjusted model ID).
  2. Forward the request, either streaming (SSE) or non-streaming.
  3. Record token usage in the rate-limiter after the response completes.
  4. Handle upstream errors gracefully (retry on 429 with backoff, surface
     other errors as properly-formatted OpenAI error objects).
  5. OOM safeguard: monitor host memory during local-fallback calls and
     trigger aggressive context truncation or a clean halt.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator

import httpx

try:
    import psutil
    _PSUTIL_AVAILABLE = True
except ImportError:
    _PSUTIL_AVAILABLE = False

from .config import AppConfig
from .context_manager import count_request_tokens, fit_messages_to_context
from .rate_limiter import RateLimiterRegistry
from .router import HybridRouter, RouteDecision
from . import storage as _storage

logger = logging.getLogger(__name__)

_CLIENT_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=5.0)


# ---------------------------------------------------------------------------
# Memory helpers
# ---------------------------------------------------------------------------

def _memory_usage_fraction() -> float:
    """Return fraction of total system RAM currently in use (0–1)."""
    if not _PSUTIL_AVAILABLE:
        return 0.0
    try:
        vm = psutil.virtual_memory()
        return vm.percent / 100.0
    except Exception:
        return 0.0


def _check_oom(config: AppConfig, is_local: bool) -> bool:
    """Return True if memory is critically high during a local inference call."""
    if not is_local:
        return False
    usage = _memory_usage_fraction()
    if usage >= config.proxy.memory_critical_threshold:
        logger.error(
            "CRITICAL: Memory usage %.0f%% exceeds threshold %.0f%%. "
            "Aborting local inference to prevent OOM.",
            usage * 100, config.proxy.memory_critical_threshold * 100,
        )
        return True
    if usage >= config.proxy.memory_warning_threshold:
        logger.warning(
            "WARNING: Memory usage %.0f%% approaching threshold %.0f%%. "
            "Context will be aggressively truncated.",
            usage * 100, config.proxy.memory_warning_threshold * 100,
        )
    return False


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------

def _sse_error(message: str, code: str = "freeclaw_error") -> bytes:
    """Format an error as an OpenAI-compatible SSE data line."""
    payload = json.dumps({
        "error": {"message": message, "type": code, "code": code}
    })
    return f"data: {payload}\n\ndata: [DONE]\n\n".encode()


def _parse_usage_from_sse(chunks: list[bytes]) -> int:
    """
    Best-effort extraction of total_tokens from the last usage chunk
    in a completed SSE stream. Returns 0 if not found.
    """
    for chunk in reversed(chunks):
        try:
            text = chunk.decode("utf-8", errors="replace")
            for line in text.split("\n"):
                line = line.strip()
                if line.startswith("data:") and line != "data: [DONE]":
                    data = json.loads(line[5:].strip())
                    usage = data.get("usage") or {}
                    if "total_tokens" in usage:
                        return int(usage["total_tokens"])
                    # Some providers put usage in the last delta
                    for choice in data.get("choices", []):
                        delta = choice.get("delta", {})
                        u = delta.get("usage") or {}
                        if "total_tokens" in u:
                            return int(u["total_tokens"])
        except Exception:
            continue
    return 0


# ---------------------------------------------------------------------------
# Core forwarding
# ---------------------------------------------------------------------------

class ForwardingProxy:
    def __init__(self, config: AppConfig, registry: RateLimiterRegistry) -> None:
        self._config = config
        self._registry = registry
        self._router = HybridRouter(config, registry)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def handle_chat_completions(
        self, payload: dict[str, Any]
    ) -> tuple[int, dict[str, str], Any]:
        """
        Route and proxy a /v1/chat/completions request.

        Returns (status_code, headers, body_or_async_generator).
        Callers must check if body is an AsyncIterator for streaming responses.
        """
        cfg = self._config

        # 1. Select route
        try:
            decision = await self._router.route(payload)
        except RuntimeError as exc:
            return 503, {}, {"error": {"message": str(exc), "type": "no_route", "code": 503}}

        # 2. OOM check for local fallback
        if decision.is_local_fallback and _check_oom(cfg, is_local=True):
            return 503, {}, {
                "error": {
                    "message": "Local inference aborted: system memory critically low.",
                    "type": "oom_error",
                    "code": 503,
                }
            }

        # 3. Context management — truncate if needed
        context_window = decision.model.context_window
        # Use more aggressive truncation under memory pressure
        if decision.is_local_fallback and _memory_usage_fraction() >= cfg.proxy.memory_warning_threshold:
            output_reserve = cfg.proxy.output_token_reserve * 2
        else:
            output_reserve = cfg.proxy.output_token_reserve

        payload = fit_messages_to_context(payload, context_window, output_reserve)

        # 4. Build upstream URL and headers
        if decision.is_local_fallback:
            upstream_url = f"{cfg.local.base_url}/v1/chat/completions"
            auth_header = "Bearer ollama"
        else:
            upstream_url = f"{decision.provider.base_url}/chat/completions"  # type: ignore
            auth_header = f"Bearer {decision.api_key}"

        # Replace model ID in payload with the upstream model ID
        adjusted_payload = dict(payload)
        adjusted_payload["model"] = decision.model.id

        headers = {
            "Authorization": auth_header,
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if payload.get("stream") else "application/json",
        }
        # OpenRouter requires these headers
        if not decision.is_local_fallback and "openrouter" in (decision.provider.name if decision.provider else ""):
            headers["HTTP-Referer"] = "https://github.com/freeclaw/freeclaw"
            headers["X-Title"] = "FreeClaw"

        is_streaming = bool(adjusted_payload.get("stream", False))

        # 5. Forward
        if is_streaming:
            return 200, {"Content-Type": "text/event-stream"}, self._stream(
                upstream_url, headers, adjusted_payload, decision
            )
        else:
            return await self._forward_sync(upstream_url, headers, adjusted_payload, decision)

    # ------------------------------------------------------------------
    # Streaming path
    # ------------------------------------------------------------------

    async def _stream(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        decision: RouteDecision,
    ) -> AsyncIterator[bytes]:
        provider_name = decision.provider.name if decision.provider else "local"
        collected: list[bytes] = []
        start = time.monotonic()

        try:
            async with httpx.AsyncClient(timeout=_CLIENT_TIMEOUT) as client:
                async with client.stream("POST", url, headers=headers, json=payload) as resp:
                    if resp.status_code == 429:
                        logger.warning(
                            "429 from %s — marking as rate-limited", provider_name
                        )
                        self._registry.record_request(provider_name, tokens_used=0)
                        _storage.record_request(provider_name, decision.model.id, is_error=True, is_local=decision.is_local_fallback)
                        yield _sse_error(
                            f"Rate limit exceeded for {provider_name}. FreeClaw will route "
                            "to another provider on the next request.",
                            code="rate_limit_exceeded",
                        )
                        return

                    if resp.status_code >= 400:
                        body = await resp.aread()
                        logger.error(
                            "Upstream %s returned HTTP %d: %s",
                            provider_name, resp.status_code, body[:500],
                        )
                        _storage.record_request(provider_name, decision.model.id, is_error=True, is_local=decision.is_local_fallback)
                        yield _sse_error(
                            f"Upstream provider '{provider_name}' returned HTTP {resp.status_code}."
                        )
                        return

                    async for chunk in resp.aiter_bytes(chunk_size=None):
                        collected.append(chunk)
                        yield chunk

        except httpx.TimeoutException:
            logger.error("Timeout streaming from %s", provider_name)
            _storage.record_request(provider_name, decision.model.id, is_error=True, is_local=decision.is_local_fallback)
            yield _sse_error(f"Request to '{provider_name}' timed out.")
            return
        except Exception as exc:
            logger.error("Error streaming from %s: %s", provider_name, exc)
            _storage.record_request(provider_name, decision.model.id, is_error=True, is_local=decision.is_local_fallback)
            yield _sse_error(f"Upstream error from '{provider_name}': {exc}")
            return

        # Record usage after stream completes
        elapsed = time.monotonic() - start
        tokens_used = _parse_usage_from_sse(collected)
        if not tokens_used:
            tokens_used = count_request_tokens(payload)
        self._registry.record_request(provider_name, tokens_used=tokens_used)
        _storage.record_request(
            provider_name, decision.model.id,
            input_tokens=count_request_tokens(payload),
            total_tokens=tokens_used,
            duration_ms=int((time.monotonic() - start) * 1000),
            is_local=decision.is_local_fallback,
        )
        logger.info(
            "Stream completed: %s/%s — %d tokens, %.1fs",
            provider_name, decision.model.id, tokens_used, elapsed,
        )

    # ------------------------------------------------------------------
    # Non-streaming path
    # ------------------------------------------------------------------

    async def _forward_sync(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        decision: RouteDecision,
    ) -> tuple[int, dict[str, str], dict]:
        provider_name = decision.provider.name if decision.provider else "local"
        start = time.monotonic()

        try:
            async with httpx.AsyncClient(timeout=_CLIENT_TIMEOUT) as client:
                resp = await client.post(url, headers=headers, json=payload)

                if resp.status_code == 429:
                    logger.warning("429 from %s — marking rate-limited", provider_name)
                    self._registry.record_request(provider_name, tokens_used=0)
                    _storage.record_request(provider_name, decision.model.id, is_error=True, is_local=decision.is_local_fallback)
                    return 429, {}, {
                        "error": {
                            "message": f"Rate limit exceeded for {provider_name}.",
                            "type": "rate_limit_exceeded",
                            "code": 429,
                        }
                    }

                data = resp.json()

                # Record usage
                usage = data.get("usage") or {}
                input_tokens = usage.get("prompt_tokens", count_request_tokens(payload))
                output_tokens = usage.get("completion_tokens", 0)
                tokens_used = usage.get("total_tokens", input_tokens + output_tokens) or input_tokens
                self._registry.record_request(provider_name, tokens_used=tokens_used)
                _storage.record_request(
                    provider_name, decision.model.id,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=tokens_used,
                    duration_ms=int((time.monotonic() - start) * 1000),
                    is_local=decision.is_local_fallback,
                )

                logger.info(
                    "Sync response: %s/%s — %d tokens, HTTP %d",
                    provider_name, decision.model.id, tokens_used, resp.status_code,
                )
                return resp.status_code, {"Content-Type": "application/json"}, data

        except httpx.TimeoutException:
            logger.error("Timeout from %s", provider_name)
            _storage.record_request(provider_name, decision.model.id, is_error=True, is_local=decision.is_local_fallback)
            return 504, {}, {"error": {"message": "Upstream timeout.", "code": 504}}
        except Exception as exc:
            logger.error("Error from %s: %s", provider_name, exc)
            return 502, {}, {"error": {"message": str(exc), "code": 502}}
