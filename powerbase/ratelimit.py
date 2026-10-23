"""In-process token-bucket rate limiting, keyed by API key id.

Deliberately in-memory: at single-node scale this is exact and costs nothing.
If Powerbase is ever run multi-process, swap the store for Redis — the
interface (``allow`` returning seconds-until-retry) does not change.
"""

from __future__ import annotations

import threading
import time


class TokenBucketLimiter:
    def __init__(self, rate_per_second: float, burst: int):
        self.rate = float(rate_per_second)
        self.burst = float(burst)
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, ts)
        self._lock = threading.Lock()

    def allow(self, key: str, cost: float = 1.0) -> tuple[bool, float]:
        """Consume ``cost`` tokens. Returns ``(allowed, retry_after_seconds)``."""
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(key, (self.burst, now))
            tokens = min(self.burst, tokens + (now - last) * self.rate)
            if tokens >= cost:
                self._buckets[key] = (tokens - cost, now)
                return True, 0.0
            deficit = cost - tokens
            retry_after = deficit / self.rate if self.rate > 0 else 1.0
            self._buckets[key] = (tokens, now)
            return False, max(retry_after, 0.001)
