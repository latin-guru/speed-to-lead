"""Token-bucket rate limiting with a pluggable state store.

The default ``InMemoryBucketStore`` is per-process. For multiple workers or instances,
implement ``BucketStore`` on Redis or the database (session 3+).
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class BucketState:
    tokens: float
    updated_at: float


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    retry_after_seconds: int = 0


class BucketStore(Protocol):
    def get(self, key: str) -> BucketState | None: ...

    def set(self, key: str, state: BucketState) -> None: ...

    def lock(self) -> threading.Lock: ...


class InMemoryBucketStore:
    def __init__(self, max_keys: int = 50_000) -> None:
        self._data: dict[str, BucketState] = {}
        self._lock = threading.Lock()
        self._max_keys = max_keys

    def get(self, key: str) -> BucketState | None:
        return self._data.get(key)

    def set(self, key: str, state: BucketState) -> None:
        if key not in self._data and len(self._data) >= self._max_keys:
            # Drop the oldest-touched entries so a flood of spoofed keys cannot exhaust memory.
            for old in sorted(self._data, key=lambda k: self._data[k].updated_at)[:1000]:
                del self._data[old]
        self._data[key] = state

    def lock(self) -> threading.Lock:
        return self._lock


class TokenBucketLimiter:
    def __init__(
        self,
        *,
        capacity: int,
        refill_per_second: float,
        store: BucketStore | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if capacity < 1 or refill_per_second <= 0:
            raise ValueError("capacity must be >= 1 and refill rate > 0")
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self.store: BucketStore = store or InMemoryBucketStore()
        self._clock = clock

    def hit(self, key: str, cost: float = 1.0) -> RateDecision:
        now = self._clock()
        with self.store.lock():
            state = self.store.get(key)
            tokens = float(self.capacity) if state is None else state.tokens
            if state is not None:
                elapsed = max(0.0, now - state.updated_at)
                tokens = min(float(self.capacity), tokens + elapsed * self.refill_per_second)
            if tokens >= cost:
                self.store.set(key, BucketState(tokens - cost, now))
                return RateDecision(True)
            self.store.set(key, BucketState(tokens, now))
            wait = (cost - tokens) / self.refill_per_second
            return RateDecision(False, max(1, math.ceil(wait)))
