from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import init_db, make_engine, make_session_factory
from app.jobs import queue
from app.models import Job

T0 = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)


@pytest.fixture
def engine() -> Iterator[Engine]:
    e = make_engine("sqlite://")
    init_db(e)
    yield e
    e.dispose()


@pytest.fixture
def sf(engine: Engine) -> sessionmaker[Session]:
    return make_session_factory(engine)


def _add(sf: sessionmaker[Session], key: str = "k1", **kw: object) -> None:
    with sf() as s:
        queue.enqueue(s, kw.pop("kind", "demo"), {"n": 1}, idempotency_key=key, now=T0, **kw)  # type: ignore[arg-type]
        s.commit()


def _job(sf: sessionmaker[Session], key: str = "k1") -> Job:
    with sf() as s:
        return s.scalars(select(Job).where(Job.idempotency_key == key)).one()


def _claim(sf: sessionmaker[Session], worker: str = "w1", now: datetime = T0) -> queue.ClaimedJob:
    job = queue.claim_next(sf, worker, now=now, lease_seconds=30)
    assert job is not None
    return job


def test_enqueue_is_idempotent(sf: sessionmaker[Session]) -> None:
    with sf() as s:
        assert queue.enqueue(s, "demo", {"n": 1}, idempotency_key="same", now=T0)
        assert not queue.enqueue(s, "demo", {"n": 2}, idempotency_key="same", now=T0)
        s.commit()
        jobs = list(s.scalars(select(Job)))
    assert len(jobs) == 1
    assert jobs[0].payload == {"n": 1}
    assert jobs[0].status == "queued"


def test_claim_sets_lease_and_counts_attempt(sf: sessionmaker[Session]) -> None:
    _add(sf)
    job = _claim(sf)
    assert job.attempts == 1
    row = _job(sf)
    assert row.status == "running"
    assert row.locked_by == "w1"
    assert row.locked_until == T0 + timedelta(seconds=30)
    assert queue.claim_next(sf, "w2", now=T0, lease_seconds=30) is None  # nothing else due


def test_claim_respects_run_at_order_and_kinds(sf: sessionmaker[Session]) -> None:
    _add(sf, "later", run_at=T0 + timedelta(minutes=5))
    _add(sf, "b", kind="other")
    _add(sf, "a")
    assert queue.claim_next(sf, "w", now=T0, lease_seconds=30, kinds=["nothing"]) is None
    first = _claim(sf)
    assert first.idempotency_key == "b"  # same run_at: lower id first
    only_demo = queue.claim_next(sf, "w", now=T0, lease_seconds=30, kinds=["demo"])
    assert only_demo is not None
    assert only_demo.idempotency_key == "a"
    assert queue.claim_next(sf, "w", now=T0, lease_seconds=30) is None  # "later" not due
    assert _claim(sf, now=T0 + timedelta(minutes=5)).idempotency_key == "later"


def test_sqlite_claim_is_atomic_when_a_candidate_is_taken(sf: sessionmaker[Session]) -> None:
    """Simulate losing the race: the row is claimed between our SELECT and our UPDATE."""
    _add(sf, "a")
    _add(sf, "b")
    real_execute = Session.execute
    stolen = {"done": False}

    def racing_execute(self: Session, stmt: object, *a: object, **kw: object) -> object:
        if not stolen["done"] and getattr(stmt, "is_update", False):
            stolen["done"] = True
            with sf() as other:  # another worker grabs job "a" first
                other.execute(
                    Job.__table__.update()
                    .where(Job.idempotency_key == "a")
                    .values(status="running", locked_by="w-other")
                )
                other.commit()
        return real_execute(self, stmt, *a, **kw)  # type: ignore[arg-type]

    Session.execute = racing_execute  # type: ignore[assignment,method-assign]
    try:
        job = queue.claim_next(sf, "w1", now=T0, lease_seconds=30)
    finally:
        Session.execute = real_execute  # type: ignore[method-assign]
    assert job is not None
    assert job.idempotency_key == "b"
    assert _job(sf, "a").locked_by == "w-other"


def test_complete_is_fenced_by_lease_owner(sf: sessionmaker[Session]) -> None:
    _add(sf)
    job = _claim(sf)
    with sf() as s:
        assert not queue.complete(s, job.id, "someone-else", now=T0)
        assert queue.complete(s, job.id, "w1", now=T0)
        s.commit()
    row = _job(sf)
    assert row.status == "done"
    assert row.locked_by is None


def test_fail_schedules_backoff_then_dead_letters(sf: sessionmaker[Session]) -> None:
    _add(sf, max_attempts=3)
    now = T0
    delays = []
    for attempt in (1, 2):
        job = _claim(sf, now=now)
        status = queue.fail(sf, job, "w1", "boom", now=now, base=10, cap=1000, rng=lambda: 0.0)
        assert status == "queued"
        row = _job(sf)
        assert row.attempts == attempt
        assert row.last_error == "boom"
        delays.append((row.run_at - now).total_seconds())
        now = row.run_at
    assert delays == [5.0, 10.0]  # equal jitter with rng=0: half of base * 2**(n-1)
    job = _claim(sf, now=now)
    assert queue.fail(sf, job, "w1", "boom", now=now, base=10, cap=1000) == "dead"
    assert _job(sf).status == "dead"
    assert queue.claim_next(sf, "w1", now=now + timedelta(days=1), lease_seconds=30) is None


def test_fail_permanent_goes_straight_to_dead(sf: sessionmaker[Session]) -> None:
    _add(sf)
    job = _claim(sf)
    assert queue.fail(sf, job, "w1", "bad", now=T0, base=1, cap=1, permanent=True) == "dead"
    assert _job(sf).attempts == 1


def test_fail_after_lease_lost_is_ignored(sf: sessionmaker[Session]) -> None:
    _add(sf)
    job = _claim(sf)
    assert queue.fail(sf, job, "not-me", "boom", now=T0, base=1, cap=1) is None
    assert _job(sf).status == "running"


@pytest.mark.parametrize("attempt", [1, 2, 3, 8, 30])
def test_backoff_bounds(attempt: int) -> None:
    lo = queue.backoff_delay(attempt, base=5, cap=300, rng=lambda: 0.0)
    hi = queue.backoff_delay(attempt, base=5, cap=300, rng=lambda: 0.999999)
    ceiling = min(300, 5 * 2 ** (attempt - 1))
    assert lo == pytest.approx(ceiling / 2)
    assert ceiling / 2 <= hi < ceiling


def test_defer_does_not_use_an_attempt(sf: sessionmaker[Session]) -> None:
    _add(sf, max_attempts=1)
    until = T0 + timedelta(hours=2)
    for _ in range(3):  # deferring many times never dead-letters the job
        job = _claim(sf, now=until)
        assert queue.defer(sf, job, "w1", until, "quiet_hours", now=T0)
    row = _job(sf)
    assert row.status == "queued"
    assert row.attempts == 0
    assert row.run_at == until
    assert row.last_error == "deferred:quiet_hours"
    assert not queue.defer(sf, job, "w1", until, "x", now=T0)  # not running any more


def test_expired_lease_is_recovered_or_dead_lettered(sf: sessionmaker[Session]) -> None:
    _add(sf, "retry", max_attempts=3)
    _add(sf, "last", max_attempts=1)
    _claim(sf, "w1")
    _claim(sf, "w1")
    assert queue.recover_expired(sf, now=T0 + timedelta(seconds=10)) == 0  # leases still valid
    assert queue.recover_expired(sf, now=T0 + timedelta(seconds=31)) == 2
    retry, last = _job(sf, "retry"), _job(sf, "last")
    assert (retry.status, retry.locked_by, retry.last_error) == ("queued", None, "lease expired")
    assert last.status == "dead"


def test_stale_worker_cannot_complete_after_takeover(sf: sessionmaker[Session]) -> None:
    _add(sf)
    stale = _claim(sf, "w1")
    later = T0 + timedelta(seconds=31)
    queue.recover_expired(sf, now=later)
    fresh = _claim(sf, "w2", now=later)
    assert fresh.attempts == 2
    with sf() as s:
        assert not queue.complete(s, stale.id, "w1", now=later)
        assert queue.complete(s, fresh.id, "w2", now=later)
        s.commit()


def test_retry_dead_resets_attempts(sf: sessionmaker[Session]) -> None:
    _add(sf, max_attempts=1)
    job = _claim(sf)
    queue.fail(sf, job, "w1", "boom", now=T0, base=1, cap=1)
    with sf() as s:
        assert queue.retry_dead(s, job.id, now=T0)
        assert not queue.retry_dead(s, job.id, now=T0)  # no longer dead
        s.commit()
    row = _job(sf)
    assert (row.status, row.attempts, row.last_error) == ("queued", 0, "boom")


def test_cancel_and_release(sf: sessionmaker[Session]) -> None:
    import uuid

    from app.models import Lead

    with sf() as s:
        lead = Lead(name="Pat Doe", source="api", created_at=T0)
        s.add(lead)
        s.flush()
        lid = lead.id
        queue.enqueue(s, "send", {}, idempotency_key="s1", lead_id=lid, now=T0)
        queue.enqueue(s, "send", {}, idempotency_key="s2", lead_id=uuid.uuid4(), now=T0)
        s.commit()
        assert queue.cancel_pending(s, lid, "send", "stop", now=T0) == 1
        s.commit()
    assert _job(sf, "s1").status == "cancelled"
    assert _job(sf, "s2").status == "queued"

    job = _claim(sf)
    queue.defer(sf, job, "w1", T0 + timedelta(hours=1), "kill_switch", now=T0)
    with sf() as s:
        assert queue.release_deferred(s, "send", "kill_switch", now=T0) == 1
        s.commit()
    assert _job(sf, "s2").run_at == T0
