"""Queue primitives over the ``jobs`` table.

Lifecycle::

    queued --claim--> running --complete--> done
       ^                 |  \\--fail (attempts left)--> queued (run_at = now + backoff)
       |                 |   \\-fail (out of attempts / permanent)--> dead
       |                 \\--defer (guard said "not yet")--> queued (attempt not counted)
       +--- lease expired (worker died) / admin retry of a dead job

Claiming:

- **Postgres**: ``SELECT ... FOR UPDATE SKIP LOCKED`` picks a due row that no other worker
  has locked, then the same transaction marks it ``running``.
- **SQLite**: no row locks, so the claim is a conditional
  ``UPDATE jobs SET status='running' ... WHERE id=:id AND status='queued'``. SQLite runs
  writes one at a time, so exactly one worker sees ``rowcount == 1``; a loser tries the next
  candidate.

Every state change after the claim is *fenced*: it only applies ``WHERE locked_by = <me> AND
status = 'running'``. A worker whose lease expired (and whose job another worker took over)
cannot overwrite the newer result.
"""

from __future__ import annotations

import logging
import random
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import CursorResult, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from app.models import Job, JobStatus, utcnow

log = logging.getLogger(__name__)

ERROR_MAX = 500


class Deferred(Exception):  # noqa: N818  (control flow, not an error)
    """Raised by a handler: "not now, try again at ``until``". Does not use up an attempt."""

    def __init__(self, until: datetime, reason: str) -> None:
        super().__init__(reason)
        self.until = until
        self.reason = reason


class PermanentJobError(Exception):
    """Raised by a handler when retrying cannot help (bad payload, unknown kind)."""


@dataclass(frozen=True)
class ClaimedJob:
    id: int
    kind: str
    payload: dict[str, Any]
    attempts: int  # including this run
    max_attempts: int
    idempotency_key: str
    lead_id: uuid.UUID | None


def _rowcount(result: object) -> int:
    assert isinstance(result, CursorResult)
    return int(result.rowcount)


def insert_ignore(session: Session, model: type[Any], values: dict[str, Any], key: str) -> bool:
    """``INSERT ... ON CONFLICT (key) DO NOTHING``. True if a row was inserted."""
    dialect = session.get_bind().dialect.name
    insert_fn = pg_insert if dialect == "postgresql" else sqlite_insert
    stmt = insert_fn(model).values(**values).on_conflict_do_nothing(index_elements=[key])
    return _rowcount(session.execute(stmt)) == 1


def enqueue(
    session: Session,
    kind: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str,
    run_at: datetime | None = None,
    max_attempts: int = 5,
    lead_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> bool:
    """Add a job unless one with the same ``idempotency_key`` already exists.

    Runs in the caller's transaction, so a job is only visible once the change that caused
    it (e.g. the new lead) commits, and is rolled back with it. Returns True if inserted.
    """
    moment = now or utcnow()
    inserted = insert_ignore(
        session,
        Job,
        {
            "kind": kind,
            "payload": payload,
            "run_at": run_at or moment,
            "attempts": 0,
            "max_attempts": max_attempts,
            "status": JobStatus.QUEUED.value,
            "idempotency_key": idempotency_key,
            "lead_id": lead_id,
            "created_at": moment,
            "updated_at": moment,
        },
        "idempotency_key",
    )
    if not inserted:
        log.info("job_enqueue_duplicate", extra={"kind": kind, "key": idempotency_key})
    return inserted


def _due(now: datetime, kinds: Iterable[str] | None) -> list[Any]:
    conds: list[Any] = [Job.status == JobStatus.QUEUED.value, Job.run_at <= now]
    if kinds is not None:
        conds.append(Job.kind.in_(list(kinds)))
    return conds


def _claimed(job: Job) -> ClaimedJob:
    return ClaimedJob(
        id=job.id,
        kind=job.kind,
        payload=dict(job.payload or {}),
        attempts=job.attempts,
        max_attempts=job.max_attempts,
        idempotency_key=job.idempotency_key,
        lead_id=job.lead_id,
    )


def _claim_values(worker_id: str, now: datetime, lease_seconds: float) -> dict[str, Any]:
    return {
        "status": JobStatus.RUNNING.value,
        "locked_by": worker_id,
        "locked_until": now + timedelta(seconds=lease_seconds),
        "attempts": Job.attempts + 1,
        "updated_at": now,
    }


def claim_next(
    session_factory: sessionmaker[Session],
    worker_id: str,
    *,
    now: datetime,
    lease_seconds: float,
    kinds: Iterable[str] | None = None,
) -> ClaimedJob | None:
    """Claim the oldest due job, or return None if nothing is due."""
    kinds = list(kinds) if kinds is not None else None
    with session_factory() as session:
        if session.get_bind().dialect.name == "postgresql":
            job_id = session.scalar(
                select(Job.id)
                .where(*_due(now, kinds))
                .order_by(Job.run_at, Job.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if job_id is None:
                session.rollback()
                return None
            session.execute(
                update(Job)
                .where(Job.id == job_id)
                .values(**_claim_values(worker_id, now, lease_seconds))
            )
            job = session.get(Job, job_id, populate_existing=True)
            assert job is not None
            claimed = _claimed(job)
            session.commit()
            return claimed

        # SQLite: optimistic, atomic conditional UPDATE.
        candidates = list(
            session.scalars(
                select(Job.id).where(*_due(now, kinds)).order_by(Job.run_at, Job.id).limit(5)
            )
        )
        for job_id in candidates:
            result = session.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == JobStatus.QUEUED.value)
                .values(**_claim_values(worker_id, now, lease_seconds))
            )
            if _rowcount(result) == 1:
                job = session.get(Job, job_id, populate_existing=True)
                assert job is not None
                claimed = _claimed(job)
                session.commit()
                return claimed
        session.rollback()
        return None


def _fenced(job_id: int, worker_id: str) -> list[Any]:
    return [Job.id == job_id, Job.locked_by == worker_id, Job.status == JobStatus.RUNNING.value]


def complete(session: Session, job_id: int, worker_id: str, *, now: datetime) -> bool:
    """Mark done inside the handler's transaction. False if this worker lost the lease."""
    result = session.execute(
        update(Job)
        .where(*_fenced(job_id, worker_id))
        .values(
            status=JobStatus.DONE.value,
            locked_by=None,
            locked_until=None,
            last_error=None,
            updated_at=now,
        )
    )
    return _rowcount(result) == 1


def backoff_delay(
    attempt: int, *, base: float, cap: float, rng: Callable[[], float] = random.random
) -> float:
    """Exponential backoff with "equal jitter": half fixed, half random.

    attempt 1 -> base * [0.5, 1), attempt 2 -> 2*base * [0.5, 1), ... capped at ``cap``.
    The fixed half guarantees real spacing; the random half spreads out retries from many
    jobs that failed together (e.g. a provider outage).
    """
    ceiling = min(cap, base * 2.0 ** max(0, attempt - 1))
    return ceiling / 2 + rng() * ceiling / 2


def fail(
    session_factory: sessionmaker[Session],
    job: ClaimedJob,
    worker_id: str,
    error: str,
    *,
    now: datetime,
    base: float,
    cap: float,
    rng: Callable[[], float] = random.random,
    permanent: bool = False,
) -> str | None:
    """Record a failure. Returns the new status (queued | dead), or None if the lease was lost."""
    dead = permanent or job.attempts >= job.max_attempts
    values: dict[str, Any] = {
        "locked_by": None,
        "locked_until": None,
        "last_error": error[:ERROR_MAX],
        "updated_at": now,
    }
    if dead:
        values["status"] = JobStatus.DEAD.value
    else:
        values["status"] = JobStatus.QUEUED.value
        values["run_at"] = now + timedelta(
            seconds=backoff_delay(job.attempts, base=base, cap=cap, rng=rng)
        )
    with session_factory() as session:
        result = session.execute(update(Job).where(*_fenced(job.id, worker_id)).values(**values))
        session.commit()
    if _rowcount(result) != 1:
        return None
    status: str = values["status"]
    log_fn = log.error if dead else log.warning
    log_fn(
        "job_dead" if dead else "job_retry_scheduled",
        extra={"job_id": job.id, "kind": job.kind, "attempts": job.attempts, "error": error[:200]},
    )
    return status


def defer(
    session_factory: sessionmaker[Session],
    job: ClaimedJob,
    worker_id: str,
    until: datetime,
    reason: str,
    *,
    now: datetime,
) -> bool:
    """Put a job back for later without counting this run as an attempt."""
    with session_factory() as session:
        result = session.execute(
            update(Job)
            .where(*_fenced(job.id, worker_id))
            .values(
                status=JobStatus.QUEUED.value,
                run_at=max(until, now),
                attempts=Job.attempts - 1,
                locked_by=None,
                locked_until=None,
                last_error=f"deferred:{reason}"[:ERROR_MAX],
                updated_at=now,
            )
        )
        session.commit()
    return _rowcount(result) == 1


def recover_expired(session_factory: sessionmaker[Session], *, now: datetime) -> int:
    """Requeue (or dead-letter) jobs whose worker died mid-run. Returns how many."""
    expired = [Job.status == JobStatus.RUNNING.value, Job.locked_until < now]
    reset = {"locked_by": None, "locked_until": None, "updated_at": now}
    with session_factory() as session:
        dead = session.execute(
            update(Job)
            .where(*expired, Job.attempts >= Job.max_attempts)
            .values(status=JobStatus.DEAD.value, last_error="lease expired", **reset)
        )
        requeued = session.execute(
            update(Job)
            .where(*expired)
            .values(status=JobStatus.QUEUED.value, run_at=now, last_error="lease expired", **reset)
        )
        session.commit()
    total = _rowcount(dead) + _rowcount(requeued)
    if total:
        log.warning(
            "job_leases_recovered",
            extra={"requeued": _rowcount(requeued), "dead": _rowcount(dead)},
        )
    return total


def retry_dead(session: Session, job_id: int, *, now: datetime) -> bool:
    """Admin action: give a dead job a fresh set of attempts. The caller commits."""
    result = session.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == JobStatus.DEAD.value)
        .values(status=JobStatus.QUEUED.value, attempts=0, run_at=now, updated_at=now)
    )
    return _rowcount(result) == 1


def cancel_pending(
    session: Session, lead_id: uuid.UUID, kind: str, reason: str, *, now: datetime
) -> int:
    """Cancel queued jobs of ``kind`` for a lead (running ones re-check guards themselves)."""
    result = session.execute(
        update(Job)
        .where(Job.lead_id == lead_id, Job.kind == kind, Job.status == JobStatus.QUEUED.value)
        .values(status=JobStatus.CANCELLED.value, last_error=reason[:ERROR_MAX], updated_at=now)
    )
    return _rowcount(result)


def release_deferred(session: Session, kind: str, reason: str, *, now: datetime) -> int:
    """Pull jobs deferred for ``reason`` forward to now (e.g. after the kill switch lifts)."""
    result = session.execute(
        update(Job)
        .where(
            Job.kind == kind,
            Job.status == JobStatus.QUEUED.value,
            Job.last_error == f"deferred:{reason}",
            Job.run_at > now,
        )
        .values(run_at=now, updated_at=now)
    )
    return _rowcount(result)
