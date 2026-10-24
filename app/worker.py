"""Job worker. Run with ``python -m app.worker`` (``--drain`` to stop when the queue is idle).

Any number of workers can run side by side; the claim protocol in ``app.jobs.queue`` makes
sure each job is run by one worker at a time.
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import signal
import socket
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from types import FrameType

import httpx
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.db import make_engine, make_session_factory, schema_is_current
from app.jobs import queue
from app.jobs.handlers import HANDLERS, Handler, JobContext
from app.logging import configure_logging
from app.models import utcnow
from app.runtime import build_job_context, build_scoring
from app.security import fields

log = logging.getLogger(__name__)


class Worker:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        ctx: JobContext,
        *,
        worker_id: str | None = None,
        lease_seconds: float = 60.0,
        backoff_base: float = 5.0,
        backoff_cap: float = 900.0,
        recover_every: timedelta = timedelta(seconds=10),
        clock: Callable[[], datetime] = utcnow,
        rng: Callable[[], float] = random.random,
        handlers: dict[str, Handler] | None = None,
        kinds: Iterable[str] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.ctx = ctx
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.lease_seconds = lease_seconds
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.recover_every = recover_every
        self.clock = clock
        self.rng = rng
        self.handlers = handlers if handlers is not None else HANDLERS
        self.kinds = list(kinds) if kinds is not None else None
        self._last_recover: datetime | None = None

    @classmethod
    def from_settings(
        cls, settings: Settings, sf: sessionmaker[Session], ctx: JobContext
    ) -> Worker:
        return cls(
            sf,
            ctx,
            lease_seconds=settings.job_lease_seconds,
            backoff_base=settings.job_backoff_base_seconds,
            backoff_cap=settings.job_backoff_cap_seconds,
        )

    def _maybe_recover(self, now: datetime) -> None:
        if self._last_recover is None or now - self._last_recover >= self.recover_every:
            queue.recover_expired(self.session_factory, now=now)
            self._last_recover = now

    def run_once(self) -> bool:
        """Claim and run one due job. False if nothing was due."""
        now = self.clock()
        self._maybe_recover(now)
        job = queue.claim_next(
            self.session_factory,
            self.worker_id,
            now=now,
            lease_seconds=self.lease_seconds,
            kinds=self.kinds,
        )
        if job is None:
            return False
        self._run(job)
        return True

    def _run(self, job: queue.ClaimedJob) -> None:
        started = time.monotonic()
        handler = self.handlers.get(job.kind)
        with self.session_factory() as session:
            try:
                if handler is None:
                    raise queue.PermanentJobError(f"no handler for kind {job.kind!r}")
                result = handler(self.ctx, session, job, self.clock())
                if not queue.complete(session, job.id, self.worker_id, now=self.clock()):
                    # Our lease expired and another worker owns the job now: drop our work.
                    session.rollback()
                    log.warning("job_lease_lost", extra={"job_id": job.id, "kind": job.kind})
                    return
                session.commit()
            except queue.Deferred as d:
                session.rollback()
                queue.defer(
                    self.session_factory, job, self.worker_id, d.until, d.reason, now=self.clock()
                )
                log.info(
                    "job_deferred",
                    extra={
                        "job_id": job.id,
                        "kind": job.kind,
                        "reason": d.reason,
                        "until": d.until.isoformat(),
                    },
                )
                return
            except queue.PermanentJobError as exc:
                session.rollback()
                self._fail(job, f"permanent: {exc}", permanent=True)
                return
            except Exception as exc:
                session.rollback()
                log.exception("job_failed", extra={"job_id": job.id, "kind": job.kind})
                self._fail(job, f"{type(exc).__name__}: {exc}")
                return
        log.info(
            "job_done",
            extra={
                "job_id": job.id,
                "kind": job.kind,
                "result": result,
                "attempt": job.attempts,
                "ms": int((time.monotonic() - started) * 1000),
            },
        )

    def _fail(self, job: queue.ClaimedJob, error: str, *, permanent: bool = False) -> None:
        queue.fail(
            self.session_factory,
            job,
            self.worker_id,
            error,
            now=self.clock(),
            base=self.backoff_base,
            cap=self.backoff_cap,
            rng=self.rng,
            permanent=permanent,
        )

    def run_until_idle(self, max_jobs: int = 10_000) -> int:
        """Run due jobs until none are left (or ``max_jobs``). Returns how many ran."""
        ran = 0
        while ran < max_jobs and self.run_once():
            ran += 1
        return ran

    def run_forever(self, stop: threading.Event, poll_seconds: float = 1.0) -> None:
        log.info("worker_started", extra={"worker_id": self.worker_id})
        while not stop.is_set():
            try:
                busy = self.run_once()
            except Exception:  # e.g. database briefly unreachable: back off, keep going
                log.exception("worker_loop_error")
                busy = False
            if not busy:
                stop.wait(poll_seconds)
        log.info("worker_stopped", extra={"worker_id": self.worker_id})


def main(argv: list[str] | None = None, *, settings: Settings | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.worker", description=__doc__)
    parser.add_argument("--drain", action="store_true", help="exit when no job is due")
    args = parser.parse_args(argv)

    settings = settings or get_settings()
    configure_logging(settings.log_level, json_output=settings.log_json)
    fields.configure(fields.from_settings(settings))
    engine = make_engine(settings.database_url)
    if settings.environment != "test" and not schema_is_current(engine):
        log.error("schema_not_current", extra={"hint": "run: alembic upgrade head"})
        return 2
    owned_client = (
        httpx.Client(timeout=settings.ai_timeout_seconds)
        if settings.ai_provider != "mock"
        else None
    )
    try:
        ctx = build_job_context(settings, scoring=build_scoring(settings, http_client=owned_client))
        worker = Worker.from_settings(settings, make_session_factory(engine), ctx)
        if args.drain:
            ran = worker.run_until_idle()
            log.info("worker_drained", extra={"jobs": ran})
            return 0
        stop = threading.Event()

        def _stop(signum: int, frame: FrameType | None) -> None:
            stop.set()

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        worker.run_forever(stop, settings.worker_poll_seconds)
        return 0
    finally:
        if owned_client is not None:
            owned_client.close()
        engine.dispose()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
