"""
FreeClawRouter – proxy.py
HTTP forwarding layer with full SSE streaming support.

Responsibilities:
  1. Build the upstream request (correct URL, auth header, adjusted model ID).
  2. Forward the request, either streaming (SSE) or non-streaming.
  3. Record token usage in the rate-limiter after the response completes.
  4. Retry with the next available provider on retryable failures
     (429, 5xx, timeout) — like an interrupt handler.  Retries happen BEFORE
     the first byte is sent to the client so the client never sees a partial
     stream.  Once streaming has started, no retry is attempted.
  5. Record provider health (success / rate-limited / error) via health.py.
  6. Handle upstream errors gracefully (surface as properly-formatted OpenAI
     error objects if all retries are exhausted).
  7. OOM safeguard: monitor host memory during local-fallback calls and
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
from .context_manager import (
    count_request_tokens,
    fit_messages_to_context,
    maybe_schedule_summary,
    summarizer as _summarizer,
)
from .rate_limiter import RateLimiterRegistry
from .router import HybridRouter, RouteDecision
from . import storage as _storage
from .health import tracker as _health

logger = logging.getLogger(__name__)

_CLIENT_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=5.0)

# HTTP status codes that are safe to retry on a different provider.
# 404 is included because providers return it for "model not found" —
# a configuration mismatch that should transparently fall through to the
# next provider rather than surfacing an error to the client.
_RETRYABLE_STATUSES = frozenset({404, 429, 500, 502, 503, 504})


# ---------------------------------------------------------------------------
# Internal sentinel / exception types
# ---------------------------------------------------------------------------

class _RetryableError(Exception):
    """Raised when a request should be retried on a different provider."""
    def __init__(self, provider: str, status: int, message: str) -> None:
        super().__init__(message)
        self.provider = provider
        self.status = status


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
# Payload sanitization
# ---------------------------------------------------------------------------

# OpenAI-proprietary fields that most third-party providers reject with 422.
# These are safe to strip — they control OpenAI-side storage/logging, not
# inference behaviour.
_OPENAI_ONLY_FIELDS = frozenset({
    "store",            # OpenAI conversation storage API
    "metadata",         # OpenAI request metadata tagging
    "service_tier",     # OpenAI service tier selection
    "prediction",       # OpenAI predicted output (Anthropic-style cache)
})


def _sanitize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Strip fields that free-tier providers don't support:
      - Top-level OpenAI-proprietary fields (store, metadata, etc.)
      - reasoning_content on assistant messages (emitted by reasoning models
        like DeepSeek R1; rejected with 422 by providers that don't support it)
    """
    result = {k: v for k, v in payload.items() if k not in _OPENAI_ONLY_FIELDS}

    # Strip reasoning_content from individual messages
    messages = result.get("messages")
    if messages:
        cleaned = [
            {k: v for k, v in m.items() if k != "reasoning_content"}
            if "reasoning_content" in m else m
            for m in messages
        ]
        if any(m is not orig for m, orig in zip(cleaned, messages)):
            result = dict(result)
            result["messages"] = cleaned

    dropped_top = [k for k in _OPENAI_ONLY_FIELDS if k in payload]
    if dropped_top:
        logger.debug("Stripped unsupported fields before forwarding: %s", dropped_top)
    return result


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------

def _sse_error(message: str, code: str = "freeclawrouter_error") -> bytes:
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

        Retry logic:
          - On a retryable failure (429 / 5xx / timeout) the failed provider is
            added to `failed_providers` and the router is called again to pick
            a different candidate.
          - Max attempts = number of configured providers + 1 (for local).
          - If every provider (including local) fails, return 502.
          - Retries only happen BEFORE the first data byte is forwarded to the
            client.  Once streaming has started there is no mid-stream retry.
        """
        cfg = self._config
        is_streaming = bool(payload.get("stream", False))

        failed_providers: set[str] = set()
        # +1 for local fallback, +1 to allow one final attempt
        max_attempts = len(cfg.providers) + 2

        for attempt in range(max_attempts):
            # 1. Select route (skip previously failed providers)
            try:
                decision = await self._router.route(payload, failed_providers=failed_providers)
            except RuntimeError as exc:
                return 503, {}, {"error": {"message": str(exc), "type": "no_route", "code": 503}}

            provider_name = decision.provider.name if decision.provider else "local"

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
            if decision.is_local_fallback and _memory_usage_fraction() >= cfg.proxy.memory_warning_threshold:
                output_reserve = cfg.proxy.output_token_reserve * 2
            else:
                output_reserve = cfg.proxy.output_token_reserve

            adjusted_payload = fit_messages_to_context(
                payload, context_window, output_reserve, _summarizer=_summarizer
            )
            adjusted_payload = _sanitize_payload(adjusted_payload)
            # Proactively schedule background summarization if conversation is
            # getting long (>60% of context).  Fire-and-forget — never blocks.
            maybe_schedule_summary(
                payload, context_window, output_reserve,
                cfg.local.base_url, cfg.local.router_model,
            )

            # 4. Build upstream URL and headers
            if decision.is_local_fallback:
                upstream_url = f"{cfg.local.base_url}/v1/chat/completions"
                auth_header = "Bearer ollama"
            else:
                upstream_url = f"{decision.provider.base_url}/chat/completions"  # type: ignore
                auth_header = f"Bearer {decision.api_key}"

            adjusted_payload = dict(adjusted_payload)
            adjusted_payload["model"] = decision.model.id

            headers = {
                "Authorization": auth_header,
                "Content-Type": "application/json",
                "Accept": "text/event-stream" if payload.get("stream") else "application/json",
            }
            if not decision.is_local_fallback and "openrouter" in (decision.provider.name if decision.provider else ""):
                headers["HTTP-Referer"] = "https://github.com/freeclawrouter/freeclawrouter"
                headers["X-Title"] = "FreeClawRouter"

            # 5. Forward — attempt the request
            if is_streaming:
                try:
                    gen = await self._try_start_stream(
                        upstream_url, headers, adjusted_payload, decision
                    )
                    # Stream started successfully — return to caller for piping
                    return 200, {"Content-Type": "text/event-stream"}, gen
                except _RetryableError as exc:
                    logger.warning(
                        "Retryable error from %s (attempt %d/%d): %s — trying next provider",
                        exc.provider, attempt + 1, max_attempts, exc,
                    )
                    failed_providers.add(exc.provider)
                    continue
            else:
                result = await self._forward_sync(
                    upstream_url, headers, adjusted_payload, decision,
                    failed_providers=failed_providers,
                )
                if result is None:
                    # Sentinel: retryable failure — loop continues
                    failed_providers.add(provider_name)
                    continue
                return result

        # All providers exhausted
        logger.error("All providers failed after %d attempts — returning 502", max_attempts)
        return 502, {}, {
            "error": {
                "message": "All available providers failed. Please try again later.",
                "type": "all_providers_failed",
                "code": 502,
            }
        }

    # ------------------------------------------------------------------
    # Streaming path
    # ------------------------------------------------------------------

    async def _try_start_stream(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        decision: RouteDecision,
    ) -> AsyncIterator[bytes]:
        """
        Probe the upstream connection and check the HTTP status BEFORE
        committing to stream.

        - If the status is retryable (429 / 5xx) → raises _RetryableError
          (no bytes have been sent to the caller yet, so a retry is safe).
        - If the status is 200 → returns an async generator that yields the
          response bytes and closes the connection when done.
        """
        provider_name = decision.provider.name if decision.provider else "local"

        client = httpx.AsyncClient(timeout=_CLIENT_TIMEOUT)
        try:
            req = client.build_request("POST", url, headers=headers, json=payload)
            resp = await client.send(req, stream=True)

            if resp.status_code in _RETRYABLE_STATUSES:
                status = resp.status_code
                await resp.aclose()
                await client.aclose()
                if status == 429:
                    _health.record_rate_limit(provider_name)
                    self._registry.record_request(provider_name, tokens_used=0)
                    _storage.record_request(provider_name, decision.model.id, is_error=True, is_local=decision.is_local_fallback)
                    logger.warning("429 from %s — rate-limited, will retry", provider_name)
                else:
                    _health.record_error(provider_name, f"HTTP {status}")
                    _storage.record_request(provider_name, decision.model.id, is_error=True, is_local=decision.is_local_fallback)
                    logger.warning("HTTP %d from %s — will retry", status, provider_name)
                raise _RetryableError(provider_name, status, f"HTTP {status} from {provider_name}")

            if resp.status_code >= 400:
                body = await resp.aread()
                await client.aclose()
                logger.error(
                    "Upstream %s returned HTTP %d: %s",
                    provider_name, resp.status_code, body[:500],
                )
                _health.record_error(provider_name, f"HTTP {resp.status_code}")
                _storage.record_request(provider_name, decision.model.id, is_error=True, is_local=decision.is_local_fallback)
                # Non-retryable 4xx — return an error stream (not a retry)
                async def _error_gen():
                    yield _sse_error(
                        f"Upstream provider '{provider_name}' returned HTTP {resp.status_code}."
                    )
                return _error_gen()

        except _RetryableError:
            raise
        except httpx.TimeoutException:
            await client.aclose()
            _health.record_error(provider_name, "timeout")
            _storage.record_request(provider_name, decision.model.id, is_error=True, is_local=decision.is_local_fallback)
            raise _RetryableError(provider_name, 504, f"Timeout connecting to {provider_name}")
        except Exception as exc:
            await client.aclose()
            _health.record_error(provider_name, str(exc))
            _storage.record_request(provider_name, decision.model.id, is_error=True, is_local=decision.is_local_fallback)
            raise _RetryableError(provider_name, 502, f"Connection error to {provider_name}: {exc}")

        # Status is OK — return a generator that streams the response
        start = time.monotonic()
        collected: list[bytes] = []

        async def _stream_gen():
            try:
                async for chunk in resp.aiter_bytes(chunk_size=None):
                    collected.append(chunk)
                    yield chunk
            except httpx.TimeoutException:
                logger.error("Timeout mid-stream from %s", provider_name)
                _health.record_error(provider_name, "mid-stream timeout")
                _storage.record_request(provider_name, decision.model.id, is_error=True, is_local=decision.is_local_fallback)
                yield _sse_error(f"Request to '{provider_name}' timed out mid-stream.")
                return
            except Exception as exc:
                logger.error("Error mid-stream from %s: %s", provider_name, exc)
                _health.record_error(provider_name, str(exc))
                _storage.record_request(provider_name, decision.model.id, is_error=True, is_local=decision.is_local_fallback)
                yield _sse_error(f"Upstream error from '{provider_name}': {exc}")
                return
            finally:
                await resp.aclose()
                await client.aclose()

            # Record usage after stream completes successfully
            elapsed = time.monotonic() - start
            tokens_used = _parse_usage_from_sse(collected)
            if not tokens_used:
                tokens_used = count_request_tokens(payload)
            self._registry.record_request(provider_name, tokens_used=tokens_used)
            _health.record_success(provider_name)
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

        return _stream_gen()

    # ------------------------------------------------------------------
    # Non-streaming path
    # ------------------------------------------------------------------

    async def _forward_sync(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        decision: RouteDecision,
        failed_providers: set[str] | None = None,
    ) -> tuple[int, dict[str, str], dict] | None:
        """
        Send a non-streaming request upstream.

        Returns a (status, headers, body) tuple on success or non-retryable error.
        Returns None as a sentinel when the failure is retryable (caller should
        add the provider to failed_providers and retry).
        """
        provider_name = decision.provider.name if decision.provider else "local"
        start = time.monotonic()

        try:
            async with httpx.AsyncClient(timeout=_CLIENT_TIMEOUT) as client:
                resp = await client.post(url, headers=headers, json=payload)

                if resp.status_code in _RETRYABLE_STATUSES:
                    if resp.status_code == 429:
                        logger.warning("429 from %s — rate-limited, will retry", provider_name)
                        _health.record_rate_limit(provider_name)
                        self._registry.record_request(provider_name, tokens_used=0)
                    else:
                        logger.warning(
                            "HTTP %d from %s — retryable, will retry",
                            resp.status_code, provider_name,
                        )
                        _health.record_error(provider_name, f"HTTP {resp.status_code}")
                    _storage.record_request(provider_name, decision.model.id, is_error=True, is_local=decision.is_local_fallback)
                    return None  # sentinel: retry

                data = resp.json()

                # Record usage
                usage = data.get("usage") or {}
                input_tokens = usage.get("prompt_tokens", count_request_tokens(payload))
                output_tokens = usage.get("completion_tokens", 0)
                tokens_used = usage.get("total_tokens", input_tokens + output_tokens) or input_tokens
                self._registry.record_request(provider_name, tokens_used=tokens_used)
                _health.record_success(provider_name)
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
            logger.error("Timeout from %s — will retry", provider_name)
            _health.record_error(provider_name, "timeout")
            _storage.record_request(provider_name, decision.model.id, is_error=True, is_local=decision.is_local_fallback)
            return None  # sentinel: retry
        except Exception as exc:
            logger.error("Connection error from %s: %s — will retry", provider_name, exc)
            _health.record_error(provider_name, str(exc))
            return None  # sentinel: retry
