"""Per-session token-bucket rate limiter.

Used to bound the rate at which a single `session_id` can drive LLM + tool
calls. Refills `rate_per_min` tokens every minute up to a hard cap of
`rate_burst`. Each turn attempt consumes one token; `acquire()` returns the
number of seconds the caller should wait before retrying, or `0.0` when a
token was granted.

Intentionally small and dependency-free — FastAPI `Depends` is not required
because the orchestrator calls it directly from the turn handler.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass


@dataclass(slots=True)
class _Bucket:
    tokens: float
    last: float


class RateLimiter:
    """Async-safe token bucket keyed by session_id."""

    def __init__(self, *, rate_per_min: int, burst: int) -> None:
        self._refill_per_sec = rate_per_min / 60.0 if rate_per_min > 0 else 0.0
        self._burst = float(max(burst, 0))
        self._buckets: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self._refill_per_sec > 0.0 and self._burst > 0.0

    async def acquire(self, session_id: str) -> float:
        """Try to take a token for `session_id`.

        Returns `0.0` on success, or the number of seconds the caller should
        wait before a token is expected to be available. When the limiter is
        disabled (rate_per_min==0 or burst==0), always returns `0.0`.
        """
        if not self.enabled:
            return 0.0
        now = time.monotonic()
        async with self._lock:
            bucket = self._buckets.get(session_id)
            if bucket is None:
                bucket = _Bucket(tokens=self._burst, last=now)
                self._buckets[session_id] = bucket
            else:
                bucket.tokens = min(
                    self._burst,
                    bucket.tokens + (now - bucket.last) * self._refill_per_sec,
                )
                bucket.last = now
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return 0.0
            needed = 1.0 - bucket.tokens
            return needed / self._refill_per_sec if self._refill_per_sec else 0.0

    async def reset(self, session_id: str | None = None) -> None:
        async with self._lock:
            if session_id is None:
                self._buckets.clear()
            else:
                self._buckets.pop(session_id, None)
