"""
FreeClawRouter – rate_limiter.py
Thread-safe, in-memory sliding-window rate limiter.

Tracks:
  - RPM  (requests per minute) — sliding 60-second window
  - RPD  (requests per day)    — calendar-day counter (UTC)
  - TPM  (tokens per minute)   — sliding 60-second window
  - TPD  (tokens per day)      — calendar-day counter (UTC)

Each (provider_name, model_id) pair has its own independent bucket.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .config import ProviderConfig, RateLimits


@dataclass
class _SlidingWindow:
    """Stores (timestamp, value) entries within a rolling time window."""
    window_seconds: int
    entries: deque = field(default_factory=deque)

    def _evict(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self.entries and self.entries[0][0] < cutoff:
            self.entries.popleft()

    def add(self, value: float = 1.0) -> None:
        now = time.monotonic()
        self._evict(now)
        self.entries.append((now, value))

    def total(self) -> float:
        self._evict(time.monotonic())
        return sum(v for _, v in self.entries)

    def count(self) -> int:
        self._evict(time.monotonic())
        return len(self.entries)


@dataclass
class _DayCounter:
    """Accumulates a count that resets at UTC midnight."""
    _date: str = ""
    _count: float = 0.0

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def add(self, value: float = 1.0) -> None:
        today = self._today()
        if today != self._date:
            self._date = today
            self._count = 0.0
        self._count += value

    def total(self) -> float:
        if self._today() != self._date:
            return 0.0
        return self._count


class ProviderBucket:
    """Rate-limit state for a single (provider, model) pair."""

    def __init__(self, rate_limits: RateLimits) -> None:
        self._rl = rate_limits
        self._lock = threading.Lock()

        self._rpm_window = _SlidingWindow(window_seconds=60)
        self._tpm_window = _SlidingWindow(window_seconds=60)
        self._rpd_counter = _DayCounter()
        self._tpd_counter = _DayCounter()

    def is_available(self, estimated_tokens: int = 0) -> bool:
        """Return True if this bucket can accept a new request right now."""
        with self._lock:
            rl = self._rl
            if rl.rpm is not None and self._rpm_window.count() >= rl.rpm:
                return False
            if rl.rpd is not None and self._rpd_counter.total() >= rl.rpd:
                return False
            if rl.tpm is not None and self._tpm_window.total() + estimated_tokens > rl.tpm:
                return False
            if rl.tpd is not None and self._tpd_counter.total() + estimated_tokens > rl.tpd:
                return False
            return True

    def record_request(self, tokens_used: int = 0) -> None:
        """Record a completed request and the tokens it consumed."""
        with self._lock:
            self._rpm_window.add(1)
            self._rpd_counter.add(1)
            if tokens_used:
                self._tpm_window.add(tokens_used)
                self._tpd_counter.add(tokens_used)

    def capacity_fraction(self) -> float:
        """Return a 0–1 score of remaining capacity (1 = fully available)."""
        with self._lock:
            fractions: list[float] = []
            rl = self._rl
            if rl.rpm:
                used = self._rpm_window.count()
                fractions.append(max(0.0, 1.0 - used / rl.rpm))
            if rl.rpd:
                used = self._rpd_counter.total()
                fractions.append(max(0.0, 1.0 - used / rl.rpd))
            if rl.tpm:
                used = self._tpm_window.total()
                fractions.append(max(0.0, 1.0 - used / rl.tpm))
            if rl.tpd:
                used = self._tpd_counter.total()
                fractions.append(max(0.0, 1.0 - used / rl.tpd))
            return min(fractions) if fractions else 1.0

    def stats(self) -> dict:
        with self._lock:
            rl = self._rl
            return {
                "rpm_used": self._rpm_window.count(),
                "rpm_limit": rl.rpm,
                "rpd_used": int(self._rpd_counter.total()),
                "rpd_limit": rl.rpd,
                "tpm_used": int(self._tpm_window.total()),
                "tpm_limit": rl.tpm,
                "tpd_used": int(self._tpd_counter.total()),
                "tpd_limit": rl.tpd,
            }

    def detailed_stats(self) -> dict:
        """
        Return per-dimension headroom percentages for scheduling decisions.
        headroom_pct = remaining capacity as a percentage (100 = fully unused).
        None means that dimension has no configured limit.
        """
        def _pct(used: float, limit: Optional[int]) -> Optional[float]:
            if limit is None or limit == 0:
                return None
            return round(max(0.0, (limit - used) / limit * 100), 1)

        with self._lock:
            rl = self._rl
            rpm_used = self._rpm_window.count()
            rpd_used = self._rpd_counter.total()
            tpm_used = self._tpm_window.total()
            tpd_used = self._tpd_counter.total()
            return {
                "rpm_used": rpm_used,
                "rpm_limit": rl.rpm,
                "rpm_headroom_pct": _pct(rpm_used, rl.rpm),
                "rpd_used": int(rpd_used),
                "rpd_limit": rl.rpd,
                "rpd_headroom_pct": _pct(rpd_used, rl.rpd),
                "tpm_used": int(tpm_used),
                "tpm_limit": rl.tpm,
                "tpm_headroom_pct": _pct(tpm_used, rl.tpm),
                "tpd_used": int(tpd_used),
                "tpd_limit": rl.tpd,
                "tpd_headroom_pct": _pct(tpd_used, rl.tpd),
            }


class RateLimiterRegistry:
    """
    Global registry of per-provider rate-limit buckets.
    One bucket per provider (all models on a provider share the same quota,
    matching how real free-tier APIs enforce limits).
    """

    def __init__(self) -> None:
        self._buckets: dict[str, ProviderBucket] = {}
        self._lock = threading.Lock()

    def register(self, provider: ProviderConfig) -> None:
        with self._lock:
            if provider.name not in self._buckets:
                self._buckets[provider.name] = ProviderBucket(provider.rate_limits)

    def get(self, provider_name: str) -> Optional[ProviderBucket]:
        return self._buckets.get(provider_name)

    def is_available(self, provider_name: str, estimated_tokens: int = 0) -> bool:
        bucket = self.get(provider_name)
        return bucket.is_available(estimated_tokens) if bucket else False

    def record_request(self, provider_name: str, tokens_used: int = 0) -> None:
        bucket = self.get(provider_name)
        if bucket:
            bucket.record_request(tokens_used)

    def capacity_fraction(self, provider_name: str) -> float:
        bucket = self.get(provider_name)
        return bucket.capacity_fraction() if bucket else 0.0

    def all_stats(self) -> dict:
        return {name: bucket.stats() for name, bucket in self._buckets.items()}
