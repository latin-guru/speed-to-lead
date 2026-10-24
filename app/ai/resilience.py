"""Retry with exponential backoff + full jitter, a time budget, and a small circuit breaker."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import TypeVar

from app.ai.base import RetryableProviderError

T = TypeVar("T")

MAX_RETRY_AFTER_SECONDS = 5.0


def backoff_delay(attempt: int, *, base: float, cap: float, rand: float) -> float:
    """Full-jitter delay before retry number ``attempt`` (1-based): U(0, min(cap, base*2^(n-1)))."""
    return rand * min(cap, base * 2.0 ** (attempt - 1))


def call_with_retries(
    fn: Callable[[], T],
    *,
    max_attempts: int = 3,
    base_delay: float = 0.25,
    max_delay: float = 2.0,
    budget_seconds: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    rng: Callable[[], float],
) -> tuple[T, int]:
    """Call ``fn``; retry only on ``RetryableProviderError``. Returns (result, attempts used).

    A ``Retry-After`` hint (429) is honored up to ``MAX_RETRY_AFTER_SECONDS``. When the next
    sleep would overrun ``budget_seconds`` the last error is raised instead of waiting.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    deadline = clock() + budget_seconds if budget_seconds is not None else None
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn(), attempt
        except RetryableProviderError as exc:
            if attempt >= max_attempts:
                raise
            delay = backoff_delay(attempt, base=base_delay, cap=max_delay, rand=rng())
            if exc.retry_after is not None:
                delay = max(delay, min(exc.retry_after, MAX_RETRY_AFTER_SECONDS))
            if deadline is not None and clock() + delay >= deadline:
                raise
            sleep(delay)


class CircuitBreaker:
    """Opens after ``threshold`` consecutive failures; half-opens after ``cooldown`` seconds."""

    def __init__(
        self, *, threshold: int, cooldown: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.threshold = threshold
        self.cooldown = cooldown
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._opened_at is not None and self._clock() - self._opened_at < self.cooldown

    def allow(self) -> bool:
        return not self.is_open

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.threshold:
                self._opened_at = self._clock()
