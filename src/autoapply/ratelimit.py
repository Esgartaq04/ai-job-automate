"""Token bucket, exponential backoff with jitter, and a per-source circuit breaker.

README section 8: keep volume human-plausible and stop hammering a source that's
consistently failing.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field


class TokenBucket:
    """Classic token bucket. `acquire` blocks until a token is available."""

    def __init__(self, rate_per_min: int, burst: int | None = None, *, clock=time.monotonic):
        if rate_per_min <= 0:
            raise ValueError("rate_per_min must be positive")
        self.rate_per_sec = rate_per_min / 60.0
        self.capacity = float(burst if burst is not None else max(1, rate_per_min // 4))
        self._tokens = self.capacity
        self._clock = clock
        self._updated = clock()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = now - self._updated
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate_per_sec)
            self._updated = now

    def try_acquire(self, tokens: float = 1.0) -> bool:
        with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    def acquire(self, tokens: float = 1.0, *, sleep=time.sleep) -> None:
        while not self.try_acquire(tokens):
            with self._lock:
                deficit = tokens - self._tokens
            sleep(max(0.01, deficit / self.rate_per_sec))


class CircuitOpen(RuntimeError):
    """Raised when a source has tripped its breaker and is still cooling down."""


@dataclass
class CircuitBreaker:
    """Trips a source after N consecutive failures; half-opens after `reset_after`."""

    failure_threshold: int = 5
    reset_after: float = 300.0
    clock: object = field(default=time.monotonic)

    _failures: int = 0
    _opened_at: float | None = None

    def _now(self) -> float:
        return self.clock()  # type: ignore[operator]

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        if self._now() - self._opened_at >= self.reset_after:
            return "half_open"
        return "open"

    def check(self) -> None:
        if self.state == "open":
            raise CircuitOpen(f"circuit open after {self._failures} consecutive failures")

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._opened_at = self._now()


def backoff_delay(attempt: int, *, base: float = 1.0, cap: float = 60.0, rng=random.random) -> float:
    """Exponential backoff with full jitter. `attempt` is 0-indexed."""
    ceiling = min(cap, base * (2**attempt))
    return rng() * ceiling


def human_pacing_delay(max_seconds: int, *, rng=random.random) -> float:
    """Jittered pause between submissions so traffic doesn't look like a metronome."""
    if max_seconds <= 0:
        return 0.0
    return rng() * max_seconds
