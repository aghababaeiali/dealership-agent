"""In-process rate limiting (Step 9, Part B4).

No Redis or other external service - CLAUDE.md's Anti-Over-Engineering
Rules and the "modular monolith, one FastAPI service" deployment target
both point the same way; a single process's own memory is sufficient
state for a fixed-window limiter. Two independent limits: per
authenticated customer, and a global cap across all traffic - either one
tripping returns 429 with Retry-After.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from threading import Lock


@dataclass
class _WindowCounter:
    window_start: float
    count: int = 0


@dataclass
class RateLimitResult:
    allowed: bool
    retry_after_seconds: float = 0.0


class RateLimiter:
    """Fixed-window counter, one window per key plus one global window.
    Not exact (a burst can straddle a window boundary at up to ~2x the
    configured rate) - a deliberate, documented simplification; an exact
    sliding-window log is more precise but is unnecessary complexity for
    a single-process limiter guarding against abuse, not billing."""

    def __init__(
        self, *, per_key_limit: int, global_limit: int, window_seconds: float = 60.0
    ) -> None:
        self._per_key_limit = per_key_limit
        self._global_limit = global_limit
        self._window_seconds = window_seconds
        self._lock = Lock()
        self._per_key_windows: dict[str, _WindowCounter] = {}
        self._global_window = _WindowCounter(window_start=time.monotonic())

    def _check_window(self, counter: _WindowCounter, limit: int, now: float) -> RateLimitResult:
        if now - counter.window_start >= self._window_seconds:
            counter.window_start = now
            counter.count = 0
        if counter.count >= limit:
            retry_after = self._window_seconds - (now - counter.window_start)
            return RateLimitResult(allowed=False, retry_after_seconds=max(retry_after, 0.0))
        counter.count += 1
        return RateLimitResult(allowed=True)

    def check(self, key: str) -> RateLimitResult:
        """Check both the per-key and global limits for `key` (e.g. a
        customer id, or "anonymous"). Both counters are only incremented
        if the request is allowed by both - a request rejected by one
        limit doesn't consume the other's budget."""
        now = time.monotonic()
        with self._lock:
            global_result = self._check_window(self._global_window, self._global_limit, now)
            if not global_result.allowed:
                return global_result

            counter = self._per_key_windows.setdefault(key, _WindowCounter(window_start=now))
            per_key_result = self._check_window(counter, self._per_key_limit, now)
            if not per_key_result.allowed:
                # Roll back the global counter's increment - it shouldn't
                # be charged for a request the per-key limit rejected.
                self._global_window.count -= 1
                return per_key_result

            return RateLimitResult(allowed=True)


_limiter: RateLimiter | None = None
_limiter_lock = Lock()


def get_rate_limiter(*, per_key_limit: int, global_limit: int) -> RateLimiter:
    """Process-wide singleton, so all requests share the same counters -
    constructed lazily from Settings on first use."""
    global _limiter
    with _limiter_lock:
        if _limiter is None:
            _limiter = RateLimiter(per_key_limit=per_key_limit, global_limit=global_limit)
        return _limiter


def reset_rate_limiter_for_tests() -> None:
    """Test-only: drop the singleton so each test starts with fresh
    counters. Never called by application code."""
    global _limiter
    with _limiter_lock:
        _limiter = None
