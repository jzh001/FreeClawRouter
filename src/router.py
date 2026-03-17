"""
FreeClawRouter – router.py
Hybrid two-tier routing engine.

Tier 1 — Fast Heuristics:
  Filter (provider, model) pairs by:
    • API key present (handled at config load time)
    • Rate limits not exhausted for the estimated token count
    • Context window large enough for the prompt

Tier 2 — Scheduling Score + Local LLM Confirmation:
  For each surviving candidate, Python pre-computes a deterministic scheduling
  score that encodes all OS-scheduling-inspired heuristics (see _score_candidate).
  The score and the full reasoning are handed to the local Ollama router model
  as a structured, rule-annotated prompt. The LLM's job is to confirm the
  Python recommendation or override it if it has a stronger reason — it does NOT
  need to invent the strategy from scratch.

  Scheduling goals (in priority order):
    1. Daily budget preservation — protect scarce RPD/TPD limits; they don't
       reset until midnight UTC. Never sacrifice daily headroom carelessly.
    2. Fast-refresh preference — prefer providers whose binding limit resets in
       seconds/minutes (RPM/TPM) over those that reset daily (RPD/TPD). Consuming
       RPM capacity is "free" because it refills automatically; consuming RPD
       capacity is a permanent daily deduction.
    3. Fairness / round-robin — spread load proportionally across all providers
       so no single provider's daily quota is exhausted while others are idle.
       Modelled after weighted fair-queuing: allocate to the provider with the
       most remaining daily budget first.
    4. Complexity matching — route heavy (tool-use, multi-turn) requests to
       models with large context windows; save them for when they're needed.
    5. Provider priority — use config priority as a final quality tiebreaker.

Fallback — Local Ollama Generation:
  When zero candidates pass the heuristic filter, route to the configured local
  Ollama fallback model and print a prominent console warning.

Agentic routing constraint (PRD §2.3):
  Routing decisions are made once per top-level request. The chosen
  (provider, model) pair is committed for the entire streaming response.
  There are no mid-stream swaps.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from .config import AppConfig, ModelConfig, ProviderConfig
from .context_manager import count_request_tokens
from .rate_limiter import RateLimiterRegistry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class RouteDecision:
    provider: Optional[ProviderConfig]
    model: ModelConfig
    is_local_fallback: bool = False

    @property
    def base_url(self) -> str:
        if self.is_local_fallback:
            return ""  # filled in by proxy from config
        return self.provider.base_url  # type: ignore[union-attr]

    @property
    def api_key(self) -> str:
        if self.is_local_fallback:
            return "ollama"
        return self.provider.api_key  # type: ignore[union-attr]


@dataclass
class CandidateInfo:
    """All scheduling metadata for one (provider, model) pair."""
    provider: ProviderConfig
    model: ModelConfig

    # Raw stats from the rate-limiter bucket
    rpm_used: int = 0
    rpm_limit: Optional[int] = None
    rpm_headroom_pct: Optional[float] = None   # None = no limit on this dimension
    rpd_used: int = 0
    rpd_limit: Optional[int] = None
    rpd_headroom_pct: Optional[float] = None
    tpm_used: int = 0
    tpm_limit: Optional[int] = None
    tpm_headroom_pct: Optional[float] = None
    tpd_used: int = 0
    tpd_limit: Optional[int] = None
    tpd_headroom_pct: Optional[float] = None

    # Derived
    score: float = 0.0
    binding_constraint: str = "none"   # "rpm" | "rpd" | "tpd" | "none"
    refresh_horizon: str = "minutes"   # "minutes" | "daily"
    daily_budget_status: str = "healthy"  # "healthy" | "caution" | "low"


# ---------------------------------------------------------------------------
# Complexity classification
# ---------------------------------------------------------------------------

def _estimate_complexity(payload: dict[str, Any]) -> str:
    """
    Classify request complexity from structural signals only.
    Never reads actual message content.
    """
    messages = payload.get("messages", [])
    has_tools = bool(payload.get("tools"))
    has_tool_calls = any(
        m.get("role") == "tool" or m.get("tool_calls") for m in messages
    )
    turn_count = sum(1 for m in messages if m.get("role") in ("user", "assistant"))
    last_user = next(
        (m for m in reversed(messages) if m.get("role") == "user"), None
    )
    user_msg_len = 0
    if last_user:
        content = last_user.get("content", "")
        user_msg_len = len(content) if isinstance(content, str) else sum(
            len(p.get("text", "")) for p in content if isinstance(p, dict)
        )

    if has_tools or has_tool_calls:
        return "complex"
    if turn_count > 6 or user_msg_len > 2000:
        return "moderate"
    return "simple"


# ---------------------------------------------------------------------------
# Scheduling score (deterministic Python — no LLM needed)
# ---------------------------------------------------------------------------

# Protected reserve threshold: avoid providers with less than this much daily headroom.
# They may still be used if no alternative exists, but they are heavily penalised.
_DAILY_RESERVE_PCT = 10.0   # never touch the last 10% of daily budget voluntarily
_DAILY_CAUTION_PCT = 25.0   # start deprioritising below 25%


def _score_candidate(info: CandidateInfo, input_tokens: int, complexity: str) -> float:
    """
    Compute a 0–100 scheduling score for a candidate.
    Higher = better choice for this request.

    Heuristics (in priority order):

    A. Daily budget preservation (highest weight, up to ±40 pts)
       Daily quotas (RPD, TPD) are a non-renewable resource. Model them like
       disk write endurance: once consumed they don't return until UTC midnight.
       Heavily penalise candidates whose daily headroom is critically low.

    B. Refresh-rate preference (up to +20 pts)
       Rate-Monotonic analogy: RPM limits refresh in <60 s (renewable CPU time);
       RPD limits are finite (non-renewable). Pure fast-refresh providers can
       be consumed freely; daily-budget providers must be rationed.

    C. Fairness / Deficit Round-Robin (up to +15 pts)
       Bonus proportional to remaining daily headroom, spreading traffic evenly
       across all providers so no single provider's daily quota is exhausted
       while others sit idle.

    D. Complexity + capability matching (up to ±15 pts)
       Match model strengths (tags) to request characteristics:
         - complex: prefer [agentic], [reasoning], large context
         - simple:  prefer [fast]; avoid burning [reasoning] on trivial tasks
         - reasoning models get extra penalty for simple requests (slow + wasteful)
         - coding/agentic models get bonus when tools are active

    E. Provider priority tiebreaker (up to +5 pts)
    """
    score = 50.0
    tags = set(info.model.tags)

    # ------------------------------------------------------------------
    # A. Daily budget preservation
    # ------------------------------------------------------------------
    daily_pcts = [
        p for p in (info.rpd_headroom_pct, info.tpd_headroom_pct)
        if p is not None
    ]
    if daily_pcts:
        min_daily = min(daily_pcts)
        if min_daily < _DAILY_RESERVE_PCT:
            score -= 40
        elif min_daily < _DAILY_CAUTION_PCT:
            penalty = ((_DAILY_CAUTION_PCT - min_daily) / _DAILY_CAUTION_PCT) * 20
            score -= penalty
        else:
            score += (min_daily / 100.0) * 10  # up to +10
    else:
        score += 20  # no daily cap → safest to consume freely

    # ------------------------------------------------------------------
    # B. Refresh-rate preference
    # ------------------------------------------------------------------
    has_daily_cap = (info.rpd_limit is not None or info.tpd_limit is not None)
    has_minute_cap = (info.rpm_limit is not None or info.tpm_limit is not None)

    if has_minute_cap and not has_daily_cap:
        score += 20
        info.refresh_horizon = "minutes"
    elif has_daily_cap and not has_minute_cap:
        score -= 5
        info.refresh_horizon = "daily"
    else:
        info.refresh_horizon = "daily"

    # ------------------------------------------------------------------
    # C. Fairness / Deficit Round-Robin
    # ------------------------------------------------------------------
    if daily_pcts:
        score += (min(daily_pcts) / 100.0) * 15  # up to +15

    # ------------------------------------------------------------------
    # D. Complexity + capability matching
    # ------------------------------------------------------------------
    ctx_k = info.model.context_window // 1024

    if complexity == "complex":
        # Tool-use / multi-step agent loops: need agentic capability + large ctx
        if "agentic" in tags:
            score += 8
        if "coding" in tags:
            score += 4   # coding models are often strong at tool-use too
        if "reasoning" in tags or info.model.is_reasoning:
            score += 5   # reasoning helps for complex multi-step tasks
        if ctx_k >= 128:
            score += 5
        elif ctx_k < 32:
            score -= 10  # too small — will fail mid-task

    elif complexity == "moderate":
        # Multi-turn or long messages: context matters, reasoning is a mild bonus
        if "reasoning" in tags or info.model.is_reasoning:
            score += 3
        if ctx_k >= 64:
            score += 3
        elif ctx_k < 16:
            score -= 5

    elif complexity == "simple":
        # Single-turn / short: prefer fast models; penalise heavy reasoning models
        if "fast" in tags:
            score += 8
        if info.model.is_reasoning:
            # Reasoning models burn extra tokens on internal CoT before replying.
            # Wasting a [reasoning] slot on a trivial task is doubly inefficient:
            # it's slower AND it consumes more tokens against daily budgets.
            score -= 10
        if ctx_k > 256:
            score -= 5   # oversized model for a trivial task — save the slot

    # ------------------------------------------------------------------
    # E. Provider priority (tiebreaker)
    # ------------------------------------------------------------------
    score += max(0, (10 - min(info.provider.priority, 10))) * 0.5  # 0–5 pts

    return round(max(0.0, min(100.0, score)), 1)


def _classify_candidate(info: CandidateInfo) -> None:
    """Populate derived fields (binding_constraint, daily_budget_status)."""
    # Binding constraint: which dimension is closest to its limit?
    headrooms = {}
    if info.rpm_headroom_pct is not None:
        headrooms["rpm"] = info.rpm_headroom_pct
    if info.rpd_headroom_pct is not None:
        headrooms["rpd"] = info.rpd_headroom_pct
    if info.tpm_headroom_pct is not None:
        headrooms["tpm"] = info.tpm_headroom_pct
    if info.tpd_headroom_pct is not None:
        headrooms["tpd"] = info.tpd_headroom_pct

    if headrooms:
        info.binding_constraint = min(headrooms, key=headrooms.__getitem__)
    else:
        info.binding_constraint = "none"

    # Daily budget status
    daily = {k: v for k, v in headrooms.items() if k in ("rpd", "tpd")}
    if not daily:
        info.daily_budget_status = "unlimited"
    else:
        min_daily = min(daily.values())
        if min_daily < _DAILY_RESERVE_PCT:
            info.daily_budget_status = "low"
        elif min_daily < _DAILY_CAUTION_PCT:
            info.daily_budget_status = "caution"
        else:
            info.daily_budget_status = "healthy"


# ---------------------------------------------------------------------------
# Structured routing prompt
# ---------------------------------------------------------------------------

# Human-readable descriptions of each tag — rendered in the capability sheet
_TAG_DESCRIPTIONS: dict[str, str] = {
    "reasoning":     "[reasoning]     Built-in chain-of-thought thinking before answering. "
                     "Best for: hard math, logic puzzles, complex code analysis, multi-step planning. "
                     "SLOWER than direct-mode — do NOT use for simple one-shot tasks.",
    "coding":        "[coding]        Specialised training for code generation, debugging, refactoring, "
                     "and software engineering tasks. Strong on SWE-Bench benchmarks.",
    "agentic":       "[agentic]       Optimised for multi-step tool use and autonomous task completion. "
                     "Best suited for OpenClaw agent loops that call many tools in sequence.",
    "fast":          "[fast]          Optimised for low latency and high throughput (small model or "
                     "hardware-accelerated). Prefer for simple lookups, quick edits, and high-volume tasks.",
    "multimodal":    "[multimodal]    Accepts image, video, or audio input in addition to text. "
                     "Currently irrelevant for text-only agent tasks.",
    "large_context": "[large_context] Context window ≥ 256 K tokens. Required when the conversation "
                     "history, file contents, or workspace context is very large.",
    "moe":           "[moe]           Mixture-of-Experts architecture: achieves high quality relative "
                     "to its active parameter count — efficient without sacrificing capability.",
    "multilingual":  "[multilingual]  Strong non-English language understanding and generation.",
}

# Which tags are most relevant for each complexity level
_COMPLEXITY_PREFERRED_TAGS: dict[str, list[str]] = {
    "complex":  ["agentic", "coding", "reasoning", "large_context"],
    "moderate": ["reasoning", "agentic"],
    "simple":   ["fast"],
}

_COMPLEXITY_AVOID_TAGS: dict[str, list[str]] = {
    "complex":  [],
    "moderate": [],
    "simple":   ["reasoning"],   # reasoning models waste tokens on simple tasks
}


def _format_headroom(pct: Optional[float], limit: Optional[int], used: int) -> str:
    if pct is None:
        return "no limit"
    remaining = max(0, (limit or 0) - used)
    return f"{pct:.0f}%  ({remaining} of {limit} remaining)"


def _build_capability_sheet(candidates: list[CandidateInfo], complexity: str) -> str:
    """
    Render a concise model capability reference for the routing prompt.
    Only includes tag definitions that actually appear in the candidate list,
    keeping the prompt compact for small models.
    """
    # Collect all tags that appear in this candidate set
    present_tags: set[str] = set()
    for info in candidates:
        present_tags.update(info.model.tags)
        if info.model.is_reasoning:
            present_tags.add("reasoning")

    if not present_tags:
        return ""

    preferred = _COMPLEXITY_PREFERRED_TAGS.get(complexity, [])
    avoid     = _COMPLEXITY_AVOID_TAGS.get(complexity, [])

    lines = ["TAG LEGEND (only tags present in options below are shown):"]
    for tag in ["reasoning", "coding", "agentic", "fast", "large_context", "moe",
                "multimodal", "multilingual"]:
        if tag not in present_tags:
            continue
        desc = _TAG_DESCRIPTIONS.get(tag, f"[{tag}]")
        hint = ""
        if tag in preferred:
            hint = "  ← USEFUL for this request"
        elif tag in avoid:
            hint = "  ← WASTEFUL for this request"
        lines.append(f"  {desc}{hint}")

    lines.append("")
    lines.append("For this request (complexity=" + complexity + "):")
    if preferred:
        lines.append(f"  Prefer models tagged: {', '.join(preferred)}")
    if avoid:
        lines.append(f"  Avoid models tagged : {', '.join(avoid)}")

    return "\n".join(lines)


def _build_routing_prompt(
    candidates: list[CandidateInfo],
    input_tokens: int,
    complexity: str,
    recommended_idx: int,
    local_model: str = "",
    adaptive_mode: bool = False,
) -> str:
    """
    Build a structured, self-contained routing prompt that encodes all
    scheduling rules and a model capability sheet explicitly.

    The LLM does NOT need to invent strategy — it only needs to apply the
    rules and confirm (or justify overriding) the Python recommendation.
    Security: contains ONLY scheduling metadata — no API keys, no user content.

    Parameters
    ----------
    adaptive_mode:
        When True, append "Local Ollama" as the final option and inject
        RULE 0 (adaptive conservation) before the other rules.  The LLM
        may choose the local option when cloud budgets are running low and
        the task is simple enough to handle locally.
    local_model:
        Ollama model name to display when adaptive_mode=True.
    """

    # ----- Capability sheet -----
    capability_sheet = _build_capability_sheet(candidates, complexity)

    # ----- Candidate blocks -----
    candidate_lines: list[str] = []
    for i, info in enumerate(candidates):
        is_rec = (i == recommended_idx)
        rec_tag = "  ← RECOMMENDED" if is_rec else ""

        horizon_label = (
            "resets in <60 s  [FAST-REFRESH]"
            if info.refresh_horizon == "minutes"
            else "resets at midnight UTC  [DAILY BUDGET]"
        )
        budget_label = {
            "healthy":   "✓ healthy",
            "caution":   "⚠ caution  (<25% left)",
            "low":       "✗ LOW  (<10% left) — PROTECTED RESERVE, avoid",
            "unlimited": "✓ no daily cap  (safest to consume freely)",
        }.get(info.daily_budget_status, info.daily_budget_status)

        # Compact tag list with reasoning flag
        tag_str = ", ".join(info.model.tags) if info.model.tags else "general"
        if info.model.is_reasoning and "reasoning" not in info.model.tags:
            tag_str += ", reasoning"

        lines = [
            f"Option {i + 1}: {info.provider.name} / {info.model.id}{rec_tag}",
            f"  Description      : {info.model.description}",
            f"  Capabilities     : {tag_str}",
            f"  Scheduling score : {info.score}/100",
            f"  Binding limit    : {info.binding_constraint}  ({horizon_label})",
            f"  Daily budget     : {budget_label}",
            f"  RPM headroom     : {_format_headroom(info.rpm_headroom_pct, info.rpm_limit, info.rpm_used)}",
            f"  RPD headroom     : {_format_headroom(info.rpd_headroom_pct, info.rpd_limit, info.rpd_used)}",
            f"  TPD headroom     : {_format_headroom(info.tpd_headroom_pct, info.tpd_limit, info.tpd_used)}",
            f"  Context window   : {info.model.context_window // 1024}K tokens",
        ]
        candidate_lines.append("\n".join(lines))

    # Append local Ollama as the final option in adaptive mode
    local_option_num = len(candidates) + 1
    if adaptive_mode and local_model:
        local_lines = [
            f"Option {local_option_num}: Local Ollama / {local_model}",
            f"  Description      : Local model running on your own hardware — zero API cost",
            f"  Capabilities     : fast",
            f"  Scheduling score : N/A  (conserves all cloud budgets)",
            f"  Daily budget     : ✓ unlimited  (local compute — no quota)",
            f"  Context window   : 128K tokens",
        ]
        candidate_lines.append("\n".join(local_lines))

    candidates_block = "\n\n".join(candidate_lines)

    # ----- RULE 0 block (adaptive mode only) -----
    rule0_block = ""
    if adaptive_mode:
        rule0_block = f"""\
RULE 0 — ADAPTIVE CONSERVATION (apply BEFORE all other rules):
  If the task complexity is "simple" AND all cloud options show daily budget
  status of "caution" or "low", choose Option {local_option_num} (Local Ollama)
  to preserve cloud credits for complex tasks that truly need advanced
  cloud-model capability.
  Analogy: route cheap jobs to the idle local processor rather than burning
  shared-network quota that cannot be recovered until midnight UTC.
  Do NOT apply this rule if any cloud option still has a "healthy" daily budget.

"""

    # ----- Full prompt -----
    prompt = f"""\
You are the routing controller for FreeClawRouter, a zero-cost LLM proxy.
Your job: choose which API endpoint handles this request.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
REQUEST SUMMARY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Input tokens : {input_tokens}
  Complexity   : {complexity}
    simple   = single-turn or short prompt
    moderate = multi-turn conversation or long message
    complex  = active tool use / multi-step agent loop (needs ≥128K context)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MODEL CAPABILITY REFERENCE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{capability_sheet}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SCHEDULING RULES — apply in this exact order
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{rule0_block}RULE 1 — HARD BLOCK:
  Never choose an option marked PROTECTED RESERVE (daily budget <10%) unless
  it is the ONLY remaining option. Exhausting daily quotas leaves the agent
  unable to run for the rest of the day.

RULE 2 — PREFER FAST-REFRESH over DAILY BUDGET:
  [FAST-REFRESH] limits reset automatically every minute — free to consume.
  [DAILY BUDGET] limits are finite and do NOT reset until midnight UTC.
  Always prefer [FAST-REFRESH] all else equal. Think: CPU time (renewable)
  vs. disk writes (non-renewable) — burn the renewable resource first.

RULE 3 — FAIRNESS (Weighted Round-Robin):
  Among similar-scoring options, pick the one with the MOST remaining daily
  headroom. This spreads load evenly so no single provider runs out while
  others are idle. Quality differences only matter as a tiebreaker.

RULE 4 — CAPABILITY MATCH (use the tag legend above):
  Match the model's capabilities to the request complexity:
  - complex  → prefer [agentic], [coding], [reasoning]; require ≥128K context
  - simple   → prefer [fast]; avoid [reasoning] models (slow + wastes tokens)
  - reasoning models should ONLY be used when the task genuinely requires
    deep thinking — they consume more tokens and are slower.

RULE 5 — QUALITY TIEBREAKER:
  Use provider priority (lower number = better quality) only when all
  other factors are equal.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
AVAILABLE OPTIONS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{candidates_block}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DECISION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The scheduler pre-computed scores using the rules above and recommends
Option {recommended_idx + 1}. Confirm unless a different option is clearly
better (reference the rule number if overriding).

Reply with ONLY the option number, e.g.: 2"""

    return prompt


# ---------------------------------------------------------------------------
# Ollama routing call
# ---------------------------------------------------------------------------

async def _ask_ollama_router(
    candidates: list[CandidateInfo],
    input_tokens: int,
    complexity: str,
    ollama_base_url: str,
    router_model: str,
    recommended_idx: int,
    timeout: float = 15.0,
    local_model: str = "",
    adaptive_mode: bool = False,
) -> int:
    """
    Send the structured routing prompt to the local Ollama model and parse
    its response. Falls back to the Python-computed recommendation on any
    error so the proxy never stalls.

    Security: the prompt contains ONLY scheduling metadata — no API keys,
    no user message content, no provider authentication data.

    Returns
    -------
    int
        Index into `candidates` for the chosen cloud provider, OR
        `len(candidates)` as a sentinel meaning "route to local Ollama"
        (only possible when adaptive_mode=True).
    """
    prompt = _build_routing_prompt(
        candidates, input_tokens, complexity, recommended_idx,
        local_model=local_model, adaptive_mode=adaptive_mode,
    )

    # Total options presented = cloud candidates + (1 if adaptive_mode else 0)
    total_options = len(candidates) + (1 if adaptive_mode and local_model else 0)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{ollama_base_url}/api/generate",
                json={
                    "model": router_model,
                    "prompt": prompt,
                    "stream": False,
                    # gpt-oss:20b uses string values "low"/"medium"/"high" for thinking,
                    # NOT boolean true/false.  "low" minimises the reasoning trace so the
                    # visible response (the option number) is reached within num_predict.
                    # Other models that don't support this field ignore it safely.
                    "think": "low",
                    "options": {
                        "temperature": 0,      # deterministic — no creativity needed
                        "num_predict": 64,     # enough for low-thinking CoT + 2-digit number
                        "top_k": 1,
                    },
                },
            )
            resp.raise_for_status()
            text = resp.json().get("response", "").strip()

            # Extract the first integer from the response (handles multi-digit numbers)
            m = re.search(r"\d+", text)
            if m:
                chosen_num = int(m.group())
                chosen_idx = chosen_num - 1  # convert 1-based to 0-based
                if 0 <= chosen_idx < len(candidates):
                    logger.debug("Ollama router chose option %d (cloud): %s", chosen_num, text[:60])
                    return chosen_idx
                if adaptive_mode and chosen_idx == len(candidates):
                    # LLM chose the local Ollama option
                    logger.info(
                        "Ollama router chose local fallback (option %d, adaptive conservation)",
                        chosen_num,
                    )
                    return len(candidates)  # sentinel: route to local
            logger.warning("Ollama router returned unparseable response: %r", text[:60])

    except Exception as exc:
        logger.warning(
            "Ollama router call failed (%s); using Python recommendation (option %d).",
            exc, recommended_idx + 1,
        )

    return recommended_idx  # fall back to Python-computed recommendation


# ---------------------------------------------------------------------------
# Cloud API routing call (router_mode = "api")
# ---------------------------------------------------------------------------

async def _ask_cloud_router(
    candidates: list[CandidateInfo],
    input_tokens: int,
    complexity: str,
    recommended_idx: int,
    providers: list[ProviderConfig],
    timeout: float = 15.0,
    adaptive_mode: bool = False,
    local_model: str = "",
) -> int:
    """
    Use a fast cloud API model to confirm / override the Python-computed
    routing recommendation.  Falls back to the Python recommendation on any
    error so the proxy never stalls.

    Model selection: prefer providers with a model tagged "fast" and the
    lowest provider priority number.  The routing request is tiny (single
    short prompt → single digit reply) so it costs negligible quota.

    Returns the chosen index into `candidates`, or len(candidates) as the
    sentinel for "route to local Ollama" (only possible in adaptive_mode).
    """
    # Pick the best available fast cloud model for routing
    router_provider: Optional[ProviderConfig] = None
    router_model_id: Optional[str] = None

    # Sort providers by priority; within each provider prefer "fast" tagged models
    sorted_providers = sorted(providers, key=lambda p: p.priority)
    for p in sorted_providers:
        fast = [m for m in p.models if "fast" in m.tags]
        if fast:
            router_provider = p
            router_model_id = sorted(fast, key=lambda m: m.priority)[0].id
            break
    if router_provider is None and sorted_providers:
        # No "fast" model found — use first model of highest-priority provider
        router_provider = sorted_providers[0]
        router_model_id = sorted(router_provider.models, key=lambda m: m.priority)[0].id

    if router_provider is None or router_model_id is None:
        logger.debug("Cloud router: no provider available, using Python recommendation.")
        return recommended_idx

    prompt = _build_routing_prompt(
        candidates, input_tokens, complexity, recommended_idx,
        local_model=local_model, adaptive_mode=adaptive_mode,
    )
    total_options = len(candidates) + (1 if adaptive_mode and local_model else 0)
    messages = [
        {
            "role": "system",
            "content": (
                "You are a routing controller. You must reply with ONLY a single integer "
                "— the option number you choose. No explanation, no punctuation, nothing else."
            ),
        },
        {"role": "user", "content": prompt},
    ]

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{router_provider.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {router_provider.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": router_model_id,
                    "messages": messages,
                    "max_tokens": 8,
                    "temperature": 0,
                    "stream": False,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()

            m = re.search(r"\d+", text)
            if m:
                chosen_num = int(m.group())
                chosen_idx = chosen_num - 1
                if 0 <= chosen_idx < len(candidates):
                    logger.debug(
                        "Cloud router (%s/%s) chose option %d",
                        router_provider.name, router_model_id, chosen_num,
                    )
                    return chosen_idx
                if adaptive_mode and chosen_idx == len(candidates):
                    logger.info(
                        "Cloud router chose local fallback (option %d)", chosen_num,
                    )
                    return len(candidates)
            logger.warning("Cloud router returned unparseable response: %r", text[:60])

    except Exception as exc:
        logger.warning(
            "Cloud router call failed (%s); using Python recommendation (option %d).",
            exc, recommended_idx + 1,
        )

    return recommended_idx


# ---------------------------------------------------------------------------
# Main router
# ---------------------------------------------------------------------------

class HybridRouter:
    def __init__(self, config: AppConfig, registry: RateLimiterRegistry) -> None:
        self._config = config
        self._registry = registry

    async def route(
        self,
        payload: dict[str, Any],
        failed_providers: set[str] | None = None,
    ) -> RouteDecision:
        """
        Determine the best (provider, model) to serve this request.

        Parameters
        ----------
        payload:
            The original /v1/chat/completions request body.
        failed_providers:
            Optional set of provider names that have already failed for this
            request (used by the retry loop in proxy.py).  These are excluded
            from Tier 1 filtering so the router can pick a fresh candidate.

        Pipeline:
          0. Local-only threshold check — short-circuit to local fallback when
             the task complexity is within the configured threshold
          1. Tier 1 — hard filter (rate limits, context window, failed providers)
          2. Tier 2 — scheduling score (Python) + LLM confirmation (Ollama)
          3. Fallback — local Ollama if all cloud providers are exhausted
        """
        cfg = self._config
        input_tokens = count_request_tokens(payload)
        output_reserve = cfg.proxy.output_token_reserve
        complexity = _estimate_complexity(payload)

        # ----- Pre-Tier-1: Local-only threshold -----
        threshold = cfg.proxy.local_only_threshold
        if (
            threshold == "always"
            or (threshold == "simple" and complexity == "simple")
            or (threshold == "moderate" and complexity in ("simple", "moderate"))
        ):
            logger.info(
                "Local-only threshold '%s' matched complexity '%s' — routing to local fallback",
                threshold, complexity,
            )
            return self._local_fallback(input_tokens)

        # ----- Tier 1: Hard heuristic filter -----
        raw_candidates: list[CandidateInfo] = []
        _failed = failed_providers or set()

        for provider, model in cfg.all_provider_models():
            # Skip providers that already failed for this request
            if provider.name in _failed:
                logger.debug(
                    "Skip %s/%s: already failed for this request",
                    provider.name, model.id,
                )
                continue

            # Context window must accommodate prompt + output reserve
            required = input_tokens + output_reserve
            if model.context_window < required:
                logger.debug(
                    "Skip %s/%s: context_window=%d < required=%d",
                    provider.name, model.id, model.context_window, required,
                )
                continue

            # Rate limits must not be currently exhausted
            if not self._registry.is_available(provider.name, input_tokens):
                logger.debug(
                    "Skip %s/%s: rate-limited (input_tokens=%d)",
                    provider.name, model.id, input_tokens,
                )
                continue

            # Collect detailed stats from the rate-limiter bucket
            bucket = self._registry.get(provider.name)
            dstats = bucket.detailed_stats() if bucket else {}

            info = CandidateInfo(
                provider=provider,
                model=model,
                rpm_used=dstats.get("rpm_used", 0),
                rpm_limit=dstats.get("rpm_limit"),
                rpm_headroom_pct=dstats.get("rpm_headroom_pct"),
                rpd_used=dstats.get("rpd_used", 0),
                rpd_limit=dstats.get("rpd_limit"),
                rpd_headroom_pct=dstats.get("rpd_headroom_pct"),
                tpm_used=dstats.get("tpm_used", 0),
                tpm_limit=dstats.get("tpm_limit"),
                tpm_headroom_pct=dstats.get("tpm_headroom_pct"),
                tpd_used=dstats.get("tpd_used", 0),
                tpd_limit=dstats.get("tpd_limit"),
                tpd_headroom_pct=dstats.get("tpd_headroom_pct"),
            )
            _classify_candidate(info)
            info.score = _score_candidate(info, input_tokens, complexity)
            raw_candidates.append(info)

        if not raw_candidates:
            return self._local_fallback(input_tokens)

        if len(raw_candidates) == 1:
            info = raw_candidates[0]
            logger.info(
                "Route → %s/%s (sole candidate, score=%.0f)",
                info.provider.name, info.model.id, info.score,
            )
            return RouteDecision(provider=info.provider, model=info.model)

        # ----- Tier 2: Scheduling score + optional LLM confirmation -----
        # Sort by score descending; Python recommendation = highest score.
        raw_candidates.sort(key=lambda c: c.score, reverse=True)
        recommended_idx = 0  # best Python score

        router_mode = cfg.proxy.router_mode  # "local" | "python" | "api"

        # Adaptive mode: let the LLM router also consider routing to local Ollama
        # when cloud budgets are running low (RULE 0 in the prompt).
        adaptive_mode = (
            threshold == "disabled"
            and cfg.local.fallback_enabled
            and len(raw_candidates) >= 2
            and router_mode in ("local", "api")
        )

        if router_mode == "python":
            # Pure Python scheduling — fastest path, no LLM call at all
            idx = recommended_idx
            router_label = "python"
        elif router_mode == "api":
            # Use a fast cloud API model to confirm / override the recommendation
            idx = await _ask_cloud_router(
                candidates=raw_candidates,
                input_tokens=input_tokens,
                complexity=complexity,
                recommended_idx=recommended_idx,
                providers=cfg.providers,
                local_model=cfg.local.fallback_model if adaptive_mode else "",
                adaptive_mode=adaptive_mode,
            )
            router_label = "cloud-api"
        else:
            # Default: "local" — use local Ollama LLM for routing decisions
            idx = await _ask_ollama_router(
                candidates=raw_candidates,
                input_tokens=input_tokens,
                complexity=complexity,
                ollama_base_url=cfg.local.base_url,
                router_model=cfg.local.router_model,
                recommended_idx=recommended_idx,
                local_model=cfg.local.fallback_model if adaptive_mode else "",
                adaptive_mode=adaptive_mode,
            )
            router_label = "local-llm"

        # Sentinel: LLM chose local Ollama (adaptive conservation)
        if idx == len(raw_candidates):
            return self._local_fallback(input_tokens)

        chosen = raw_candidates[idx]
        logger.info(
            "Route → %s/%s  (score=%.0f, router=%s, decision=%s, complexity=%s, %d candidates)",
            chosen.provider.name, chosen.model.id, chosen.score,
            router_label,
            "confirmed" if idx == recommended_idx else f"overrode to #{idx + 1}",
            complexity,
            len(raw_candidates),
        )
        return RouteDecision(provider=chosen.provider, model=chosen.model)

    def _local_fallback(self, input_tokens: int) -> RouteDecision:
        cfg = self._config
        if not cfg.local.fallback_enabled or cfg.local.fallback_model == "none":
            raise RuntimeError(
                "All free-tier API rate limits exhausted and local fallback is disabled. "
                "Enable a local Ollama model in the dashboard Settings tab or add more "
                "API provider keys."
            )

        border = "=" * 70
        logger.warning("\n%s", border)
        logger.warning(
            "  ⚠  ALL FREE-TIER API LIMITS EXHAUSTED — RUNNING ON LOCAL COMPUTE  ⚠"
        )
        logger.warning("  Model  : %s (Ollama)", cfg.local.fallback_model)
        logger.warning(
            "  Tokens : ~%d input tokens will be processed by your GPU/CPU", input_tokens
        )
        logger.warning(
            "  Tip    : Limits reset on a per-minute / per-day basis. "
            "Consider waiting or adding more API providers."
        )
        logger.warning("%s\n", border)

        from .config import ModelConfig as MC
        fallback_model = MC(
            id=cfg.local.fallback_model,
            context_window=131072,
            description="Local Ollama fallback",
        )
        return RouteDecision(
            provider=None,
            model=fallback_model,
            is_local_fallback=True,
        )
