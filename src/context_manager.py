"""
FreeClawRouter – context_manager.py
Token counting, context-window management, and background conversation summarization.

Context-fitting strategy when a prompt is too large for the chosen model:
  0. If a cached summary exists for the older portion of the conversation,
     inject it as a compressed-history system message — no information silently
     discarded.
  1. Drop oldest *middle* messages first (preserve system prompt + recent turns).
  2. Truncate the content of the oldest remaining non-system message.
  3. If still over limit (e.g. system prompt alone is too large), truncate the
     system prompt content with a visible warning prefix.

Background summarization (off the critical path):
  When a conversation exceeds SUMMARY_TRIGGER_PCT of the chosen context window,
  a background asyncio.create_task() is scheduled to call the local Ollama model
  and summarize the older messages.  The summary is cached in memory and used in
  Step 0 of the next over-limit request — turning a silent drop into a lossless
  compression.  The Ollama call is never on the request-handling critical path.

Token counting uses tiktoken when available (cl100k_base is a good approximation
for most current models). Falls back to chars/4 if tiktoken is not installed.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Summarization tuning constants
# ---------------------------------------------------------------------------

# Start background summarization when prompt exceeds this fraction of the
# effective context window (window - output_reserve).  At 0.60 the summary
# is usually ready before the conversation actually overflows.
SUMMARY_TRIGGER_PCT: float = 0.60

# Number of most-recent non-system messages always kept verbatim.
# Everything older is eligible for background summarization.
RECENT_TURNS_TO_KEEP: int = 6


# ---------------------------------------------------------------------------
# Token counting helpers
# ---------------------------------------------------------------------------

_ENCODING = None


def _get_encoding():
    global _ENCODING
    if _ENCODING is None:
        try:
            import tiktoken
            _ENCODING = tiktoken.get_encoding("cl100k_base")
        except Exception:
            _ENCODING = False  # sentinel: tiktoken not available
    return _ENCODING


def count_tokens(text: str) -> int:
    """Approximate token count for a string."""
    enc = _get_encoding()
    if enc:
        try:
            return len(enc.encode(text, disallowed_special=()))
        except Exception:
            pass
    # Fallback: ~4 chars per token
    return max(1, math.ceil(len(text) / 4))


def count_message_tokens(message: dict[str, Any]) -> int:
    """Approximate token count for a single message dict."""
    total = 4  # overhead per message (role + delimiters)
    role = message.get("role", "")
    total += count_tokens(role)
    content = message.get("content", "")
    if isinstance(content, str):
        total += count_tokens(content)
    elif isinstance(content, list):
        # Multi-part content (e.g. vision messages)
        for part in content:
            if isinstance(part, dict) and "text" in part:
                total += count_tokens(part["text"])
    # Tool calls in assistant messages
    for tc in message.get("tool_calls", []) or []:
        fn = tc.get("function", {})
        total += count_tokens(fn.get("name", ""))
        total += count_tokens(fn.get("arguments", ""))
    # Tool result messages
    if role == "tool":
        total += count_tokens(str(message.get("name", "")))
    return total


def count_request_tokens(payload: dict[str, Any]) -> int:
    """Count total input tokens for a /v1/chat/completions request payload."""
    messages: list[dict] = payload.get("messages", [])
    total = 3  # every reply is primed with <|start|>assistant<|message|>
    for msg in messages:
        total += count_message_tokens(msg)
    # Tools schema also consumes tokens
    for tool in payload.get("tools", []) or []:
        fn = tool.get("function", {})
        total += count_tokens(str(fn.get("description", "")))
        total += count_tokens(str(fn.get("parameters", "")))
    return total


# ---------------------------------------------------------------------------
# Background conversation summarizer
# ---------------------------------------------------------------------------

class ConversationSummarizer:
    """
    Off-critical-path summarizer for long conversation histories.

    Usage pattern
    -------------
    After each successful request, call ``maybe_schedule_summary()`` (the
    module-level helper) to proactively kick off summarization before the
    context overflows.  When ``fit_messages_to_context`` later needs to drop
    old messages it will check this cache first and inject a compact summary
    instead of silently discarding turns.

    All Ollama calls run inside ``asyncio.create_task()`` and are never awaited
    on the request path — the worst case is a cache miss that falls back to the
    existing drop behaviour.
    """

    _MAX_CACHE = 256  # maximum number of summaries to keep in memory

    def __init__(self) -> None:
        self._cache: dict[str, str] = {}   # message_hash → summary text
        self._pending: set[str] = set()    # hashes currently being computed

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get_summary(self, messages: list[dict]) -> Optional[str]:
        """Return the cached summary for these messages, or None."""
        return self._cache.get(self._hash(messages))

    def schedule(self, messages: list[dict], ollama_url: str, model: str) -> None:
        """
        Schedule background summarization for *messages* if not already cached
        or in-flight.  Safe to call from any async context; silently skips if
        no event loop is running (e.g. unit tests).
        """
        if not messages:
            return
        key = self._hash(messages)
        if key in self._cache or key in self._pending:
            return
        self._pending.add(key)
        try:
            asyncio.get_running_loop().create_task(
                self._run(key, messages, ollama_url, model),
                name="freeclawrouter-summarize",
            )
            logger.debug(
                "Scheduled background summarization for %d messages", len(messages)
            )
        except RuntimeError:
            # No running event loop (e.g. test environment) — skip gracefully
            self._pending.discard(key)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _hash(messages: list[dict]) -> str:
        raw = json.dumps(messages, sort_keys=True, default=str).encode()
        return hashlib.md5(raw).hexdigest()

    async def _run(
        self, key: str, messages: list[dict], ollama_url: str, model: str
    ) -> None:
        """Background coroutine: call Ollama, cache result."""
        try:
            summary = await _call_ollama_summarize(messages, ollama_url, model)
            if summary:
                self._cache[key] = summary
                # Evict oldest entry when cache is full (dict preserves insertion order)
                while len(self._cache) > self._MAX_CACHE:
                    del self._cache[next(iter(self._cache))]
                logger.debug(
                    "Conversation summary cached (%d chars, cache size=%d)",
                    len(summary), len(self._cache),
                )
        except Exception as exc:
            logger.warning("Background summarization failed: %s", exc)
        finally:
            self._pending.discard(key)


async def _call_ollama_summarize(
    messages: list[dict],
    ollama_url: str,
    model: str,
    timeout: float = 60.0,
) -> str:
    """
    Call the local Ollama model to produce a concise summary of *messages*.
    Returns the summary string, or empty string on failure.
    """
    lines: list[str] = []
    for m in messages:
        role = m.get("role", "unknown").capitalize()
        content = m.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") for p in content if isinstance(p, dict)
            )
        if content:
            # Cap per-message length to avoid huge prompts
            lines.append(f"{role}: {str(content)[:2000]}")

    if not lines:
        return ""

    transcript = "\n".join(lines)
    prompt = (
        "Summarize the following conversation history concisely. "
        "Preserve all important facts, decisions, code snippets, file names, "
        "tool calls, and context needed to continue the conversation. "
        "Output only the summary — no preamble, no commentary.\n\n"
        f"Conversation:\n{transcript}\n\nSummary:"
    )

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{ollama_url}/api/generate",
                json={"model": model, "prompt": prompt, "stream": False},
            )
            resp.raise_for_status()
            return resp.json().get("response", "").strip()
    except Exception as exc:
        logger.warning("Ollama summarize call failed: %s", exc)
        return ""


# Module-level singleton — imported by proxy.py
summarizer = ConversationSummarizer()


# ---------------------------------------------------------------------------
# Helpers for splitting messages
# ---------------------------------------------------------------------------

def _split_messages_for_summary(
    non_system: list[dict],
    keep_recent: int = RECENT_TURNS_TO_KEEP,
) -> tuple[list[dict], list[dict]]:
    """
    Split *non_system* into (old_to_summarize, recent_to_keep).
    Always keeps the last *keep_recent* messages verbatim.
    Returns ([], non_system) if there are not enough messages to split.
    """
    if len(non_system) <= keep_recent:
        return [], list(non_system)
    split_at = len(non_system) - keep_recent
    return non_system[:split_at], non_system[split_at:]


def maybe_schedule_summary(
    payload: dict[str, Any],
    context_window: int,
    output_reserve: int,
    ollama_url: str,
    router_model: str,
) -> None:
    """
    Proactively schedule background summarization when the conversation is
    approaching the context limit (>= SUMMARY_TRIGGER_PCT full).

    Intended to be called once after every successful request so that the
    summary is warm in the cache before the conversation overflows.
    Completely off the critical path — schedules a task and returns immediately.
    """
    effective_limit = context_window - output_reserve
    if effective_limit <= 0:
        return

    current_tokens = count_request_tokens(payload)
    if current_tokens < effective_limit * SUMMARY_TRIGGER_PCT:
        return  # conversation is comfortably short — nothing to do

    messages = payload.get("messages", [])
    non_system = [m for m in messages if m.get("role") != "system"]
    old_msgs, _ = _split_messages_for_summary(non_system)

    if old_msgs:
        summarizer.schedule(old_msgs, ollama_url, router_model)


# ---------------------------------------------------------------------------
# Context truncation helpers
# ---------------------------------------------------------------------------

def _truncate_content(content: str | list, max_tokens: int) -> str | list:
    """Truncate message content to fit within max_tokens."""
    if isinstance(content, list):
        # Flatten multi-part to a single text block for simplicity
        text = " ".join(
            p.get("text", "") for p in content if isinstance(p, dict) and "text" in p
        )
        content = text

    enc = _get_encoding()
    if enc:
        try:
            tokens = enc.encode(content, disallowed_special=())
            if len(tokens) > max_tokens:
                content = enc.decode(tokens[:max_tokens]) + " [truncated]"
            return content
        except Exception:
            pass

    # Fallback: character-based truncation
    char_limit = max_tokens * 4
    if len(content) > char_limit:
        content = content[:char_limit] + " [truncated]"
    return content


# ---------------------------------------------------------------------------
# Main context-fitting entry point
# ---------------------------------------------------------------------------

def fit_messages_to_context(
    payload: dict[str, Any],
    context_window: int,
    output_reserve: int = 4096,
    _summarizer: Optional[ConversationSummarizer] = None,
) -> dict[str, Any]:
    """
    Return a copy of *payload* whose messages fit within
    ``context_window - output_reserve`` tokens.

    The function modifies a copy; it never mutates the original payload.

    Parameters
    ----------
    _summarizer:
        Optional ``ConversationSummarizer`` instance.  When provided and a
        cached summary exists for the older portion of the conversation, it is
        injected as a compressed-history system message instead of dropping
        those turns silently.  Background summarization is also scheduled for
        the next call if no cached summary exists yet.
    """
    effective_limit = context_window - output_reserve
    if effective_limit <= 0:
        logger.error(
            "context_window=%d is too small for output_reserve=%d",
            context_window, output_reserve,
        )
        return payload

    current_tokens = count_request_tokens(payload)
    if current_tokens <= effective_limit:
        return payload  # already fits

    import copy
    payload = copy.deepcopy(payload)
    messages: list[dict] = payload["messages"]

    logger.warning(
        "Prompt is %d tokens, context limit is %d (window=%d minus reserve=%d). "
        "Compressing conversation history.",
        current_tokens, effective_limit, context_window, output_reserve,
    )

    # Separate system messages from the rest
    system_msgs = [m for m in messages if m.get("role") == "system"]
    non_system = [m for m in messages if m.get("role") != "system"]

    # --- Step 0: Use cached summary instead of dropping (lossless compression) ---
    if _summarizer is not None and non_system:
        old_msgs, recent_msgs = _split_messages_for_summary(non_system)
        if old_msgs:
            cached = _summarizer.get_summary(old_msgs)
            if cached:
                logger.info(
                    "Injecting cached conversation summary (%d chars) — "
                    "replacing %d old messages with compressed history.",
                    len(cached), len(old_msgs),
                )
                summary_msg: dict[str, Any] = {
                    "role": "system",
                    "content": "[Earlier conversation — compressed]\n\n" + cached,
                }
                non_system = [summary_msg] + recent_msgs
                payload["messages"] = system_msgs + non_system
                current_tokens = count_request_tokens(payload)
                if current_tokens <= effective_limit:
                    return payload
                # Summary + recent still too large — fall through to drop
            else:
                # No cached summary yet — maybe_schedule_summary() handles
                # scheduling with the correct Ollama URL/model after this call.
                pass

    # --- Step 1: Drop oldest non-system messages from the middle ---
    while non_system and count_request_tokens({**payload, "messages": system_msgs + non_system}) > effective_limit:
        dropped = non_system.pop(0)
        logger.debug(
            "Dropped message role=%s tokens≈%d", dropped.get("role"), count_message_tokens(dropped)
        )

    payload["messages"] = system_msgs + non_system
    current_tokens = count_request_tokens(payload)
    if current_tokens <= effective_limit:
        return payload

    # --- Step 2: Truncate content of oldest remaining non-system message ---
    if non_system:
        target_msg = non_system[0]
        excess = current_tokens - effective_limit
        msg_tokens = count_message_tokens(target_msg)
        allowed = max(10, msg_tokens - excess)
        target_msg["content"] = _truncate_content(target_msg.get("content", ""), allowed)
        payload["messages"] = system_msgs + non_system
        current_tokens = count_request_tokens(payload)

    if current_tokens <= effective_limit:
        return payload

    # --- Step 3: Truncate the system message(s) as last resort ---
    if system_msgs:
        logger.warning(
            "System prompt alone exceeds context limit; truncating system message. "
            "This may degrade agent behavior."
        )
        target = system_msgs[0]
        excess = current_tokens - effective_limit
        msg_tokens = count_message_tokens(target)
        allowed = max(50, msg_tokens - excess)
        target["content"] = "[System prompt truncated due to context limit]\n" + _truncate_content(
            target.get("content", ""), allowed
        )
        payload["messages"] = system_msgs + non_system

    return payload
