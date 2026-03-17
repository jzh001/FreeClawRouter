"""
FreeClawRouter – health.py
Provider health tracker — derives status from proxy request outcomes only.
Zero additional API calls are made; all state is inferred from recorded events.

Status values:
  "active"       — last request succeeded
  "rate_limited" — last request was rate-limited (HTTP 429)
  "error"        — 2+ consecutive errors
  "idle"         — not seen in the last 10 minutes
"""
from __future__ import annotations

import time
from threading import Lock
from typing import Any

# A provider that hasn't been seen in this many seconds becomes "idle"
_IDLE_SECONDS = 600  # 10 minutes

# Number of consecutive errors before the status flips to "error"
_ERROR_THRESHOLD = 2


class ProviderHealth:
    """Mutable health record for a single provider."""

    __slots__ = (
        "status",
        "last_seen_ok",
        "consecutive_errors",
        "last_event_ts",
    )

    def __init__(self) -> None:
        self.status: str = "idle"
        self.last_seen_ok: float | None = None
        self.consecutive_errors: int = 0
        self.last_event_ts: float | None = None


class HealthTracker:
    """
    Thread-safe, module-level singleton for tracking provider health.

    Usage:
        from .health import tracker
        tracker.record_success("cerebras")
        tracker.record_rate_limit("groq")
        tracker.record_error("openrouter", "connection refused")
        data = tracker.get_all()
    """

    def __init__(self) -> None:
        self._records: dict[str, ProviderHealth] = {}
        self._lock = Lock()

    # ------------------------------------------------------------------
    # Recording methods (called by proxy.py on every request outcome)
    # ------------------------------------------------------------------

    def record_success(self, provider: str) -> None:
        """Mark a provider as having just completed a successful request."""
        with self._lock:
            rec = self._get_or_create(provider)
            rec.status = "active"
            rec.consecutive_errors = 0
            rec.last_seen_ok = time.time()
            rec.last_event_ts = time.time()

    def record_rate_limit(self, provider: str) -> None:
        """Mark a provider as rate-limited (HTTP 429 received)."""
        with self._lock:
            rec = self._get_or_create(provider)
            rec.status = "rate_limited"
            # Not counting a 429 as a consecutive "error" — it's a quota event
            rec.consecutive_errors = 0
            rec.last_event_ts = time.time()

    def record_error(self, provider: str, msg: str = "") -> None:
        """
        Record a non-429 failure (5xx, timeout, connection error).
        Status flips to "error" after _ERROR_THRESHOLD consecutive failures.
        """
        with self._lock:
            rec = self._get_or_create(provider)
            rec.consecutive_errors += 1
            rec.last_event_ts = time.time()
            if rec.consecutive_errors >= _ERROR_THRESHOLD:
                rec.status = "error"
            # If below threshold, leave the previous status intact so one
            # transient failure doesn't immediately mark the provider as broken.

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_all(self) -> dict[str, dict[str, Any]]:
        """
        Return a snapshot of health state for all known providers.
        Applies the idle-timeout rule at read time (no background task needed).
        """
        now = time.time()
        with self._lock:
            out: dict[str, dict[str, Any]] = {}
            for name, rec in self._records.items():
                status = rec.status
                # Apply idle timeout: if no event in the last _IDLE_SECONDS,
                # downgrade to "idle" regardless of stored status.
                if rec.last_event_ts is not None and (now - rec.last_event_ts) > _IDLE_SECONDS:
                    status = "idle"
                out[name] = {
                    "status": status,
                    "last_seen_ok": rec.last_seen_ok,
                    "consecutive_errors": rec.consecutive_errors,
                }
            return out

    def get_status(self, provider: str) -> str:
        """Return the current status string for a single provider."""
        return self.get_all().get(provider, {}).get("status", "idle")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_or_create(self, provider: str) -> ProviderHealth:
        if provider not in self._records:
            self._records[provider] = ProviderHealth()
        return self._records[provider]


# ---------------------------------------------------------------------------
# Module-level singleton — import and use directly:
#   from .health import tracker
# ---------------------------------------------------------------------------
tracker = HealthTracker()
