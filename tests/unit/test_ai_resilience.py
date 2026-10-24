import pytest

from app.ai.base import ProviderError, RetryableProviderError
from app.ai.resilience import CircuitBreaker, backoff_delay, call_with_retries
from tests.unit.ai_helpers import FakeClock


class Flaky:
    def __init__(self, failures: list[Exception]) -> None:
        self.failures = failures
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return "ok"


def test_backoff_is_exponential_and_capped() -> None:
    assert backoff_delay(1, base=0.25, cap=2.0, rand=1.0) == 0.25
    assert backoff_delay(2, base=0.25, cap=2.0, rand=1.0) == 0.5
    assert backoff_delay(3, base=0.25, cap=2.0, rand=1.0) == 1.0
    assert backoff_delay(10, base=0.25, cap=2.0, rand=1.0) == 2.0
    assert backoff_delay(3, base=0.25, cap=2.0, rand=0.5) == 0.5  # jitter scales


def test_retries_then_succeeds_with_jittered_sleeps() -> None:
    clock = FakeClock()
    sleeps: list[float] = []

    def sleep(s: float) -> None:
        sleeps.append(s)
        clock.sleep(s)

    fn = Flaky([RetryableProviderError("503"), RetryableProviderError("timeout")])
    result, attempts = call_with_retries(
        fn, max_attempts=3, sleep=sleep, clock=clock, rng=lambda: 0.5
    )
    assert (result, attempts, fn.calls) == ("ok", 3, 3)
    assert sleeps == [0.125, 0.25]


def test_gives_up_after_max_attempts() -> None:
    sleeps: list[float] = []
    fn = Flaky([RetryableProviderError("x")] * 5)
    with pytest.raises(RetryableProviderError):
        call_with_retries(fn, max_attempts=3, sleep=sleeps.append, rng=lambda: 0.0)
    assert fn.calls == 3
    assert len(sleeps) == 2


def test_non_retryable_is_not_retried() -> None:
    fn = Flaky([ProviderError("401")])
    with pytest.raises(ProviderError):
        call_with_retries(fn, max_attempts=3, sleep=lambda s: None, rng=lambda: 0.0)
    assert fn.calls == 1


def test_retry_after_is_honored_but_capped() -> None:
    sleeps: list[float] = []
    fn = Flaky(
        [
            RetryableProviderError("429", retry_after=2.0),
            RetryableProviderError("429", retry_after=60),
        ]
    )
    call_with_retries(fn, max_attempts=3, sleep=sleeps.append, rng=lambda: 0.0)
    assert sleeps == [2.0, 5.0]


def test_budget_stops_retrying_early() -> None:
    clock = FakeClock()
    sleeps: list[float] = []
    fn = Flaky([RetryableProviderError("429", retry_after=3.0)] * 3)
    with pytest.raises(RetryableProviderError):
        call_with_retries(
            fn,
            max_attempts=3,
            budget_seconds=2.0,
            sleep=sleeps.append,
            clock=clock,
            rng=lambda: 0.0,
        )
    assert fn.calls == 1
    assert sleeps == []


def test_invalid_attempts() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        call_with_retries(lambda: 1, max_attempts=0, rng=lambda: 0.0)


def test_circuit_breaker_opens_and_half_opens() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(threshold=2, cooldown=30, clock=clock)
    assert breaker.allow()
    breaker.record_failure()
    assert breaker.allow()
    breaker.record_failure()
    assert not breaker.allow()
    clock.now += 31
    assert breaker.allow()
    breaker.record_success()
    assert breaker.allow()
