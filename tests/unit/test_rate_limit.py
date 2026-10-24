import pytest

from app.security.rate_limit import InMemoryBucketStore, TokenBucketLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_bucket_allows_capacity_then_blocks_with_retry_after() -> None:
    clock = FakeClock()
    limiter = TokenBucketLimiter(capacity=3, refill_per_second=0.5, clock=clock)
    assert all(limiter.hit("ip:1").allowed for _ in range(3))
    blocked = limiter.hit("ip:1")
    assert not blocked.allowed
    assert blocked.retry_after_seconds == 2  # one token at 0.5/s


def test_bucket_refills_over_time() -> None:
    clock = FakeClock()
    limiter = TokenBucketLimiter(capacity=2, refill_per_second=1.0, clock=clock)
    limiter.hit("k")
    limiter.hit("k")
    assert not limiter.hit("k").allowed
    clock.now += 1.0
    assert limiter.hit("k").allowed
    clock.now += 100
    assert limiter.hit("k").allowed
    assert limiter.hit("k").allowed
    assert not limiter.hit("k").allowed  # never exceeds capacity


def test_keys_are_independent() -> None:
    limiter = TokenBucketLimiter(capacity=1, refill_per_second=0.01, clock=FakeClock())
    assert limiter.hit("a").allowed
    assert not limiter.hit("a").allowed
    assert limiter.hit("b").allowed


def test_store_is_pluggable_and_bounded() -> None:
    store = InMemoryBucketStore(max_keys=1000)
    limiter = TokenBucketLimiter(capacity=1, refill_per_second=1, store=store, clock=FakeClock())
    for i in range(1500):
        limiter.hit(f"k{i}")
    assert len(store._data) <= 1000


def test_invalid_config() -> None:
    with pytest.raises(ValueError, match="capacity"):
        TokenBucketLimiter(capacity=0, refill_per_second=1)
