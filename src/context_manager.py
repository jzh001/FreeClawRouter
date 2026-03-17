"""
FreeClaw – context_manager.py
Token counting and context-window management.

Strategy when a prompt is too large for the chosen model:
  1. Drop oldest *middle* messages first (preserve system prompt + recent turns).
  2. Truncate the content of the oldest remaining non-system message.
  3. If still over limit (e.g. system prompt alone is too large), truncate the
     system prompt content with a visible warning prefix.

Token counting uses tiktoken when available (cl100k_base is a good approximation
for most current models). Falls back to chars/4 if tiktoken is not installed.
"""
from __future__ import annotations

import logging
import math
from typing import Any

logger = logging.getLogger(__name__)

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
# Context truncation
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


def fit_messages_to_context(
    payload: dict[str, Any],
    context_window: int,
    output_reserve: int = 4096,
) -> dict[str, Any]:
    """
    Return a copy of `payload` whose messages fit within
    `context_window - output_reserve` tokens.

    The function modifies a copy; it never mutates the original payload.
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
        "Truncating conversation history.",
        current_tokens, effective_limit, context_window, output_reserve,
    )

    # Separate system messages from the rest
    system_msgs = [m for m in messages if m.get("role") == "system"]
    non_system = [m for m in messages if m.get("role") != "system"]

    # --- Step 1: Drop oldest non-system messages from the middle ---
    # Keep the most recent turns; drop from the front of non_system.
    while non_system and count_request_tokens({**payload, "messages": system_msgs + non_system}) > effective_limit:
        dropped = non_system.pop(0)
        logger.debug("Dropped message role=%s tokens≈%d", dropped.get("role"), count_message_tokens(dropped))

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
