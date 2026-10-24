"""Worker end to end: API capture -> queue -> worker(s) -> outbox / scores / CRM."""

import os
import threading
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.jobs import queue
from app.main import create_app
from app.models import Activity, Job, Lead, OutboxMessage
from app.worker import Worker, main
from tests.conftest import make_settings, make_worker, valid_lead

PG_URL = os.environ.get("TEST_POSTGRES_URL")


def _run_concurrently(app: Any, n_workers: int) -> None:
    errors: list[BaseException] = []

    def work(i: int) -> None:
        try:
            make_worker(app, worker_id=f"w{i}").run_until_idle()
        except BaseException as exc:  # surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors


def _capture_and_drain(url: str, n_leads: int, n_workers: int) -> Any:
    app = create_app(make_settings(database_url=url, rate_limit_capacity=100))
    with TestClient(app) as c:
        for i in range(n_leads):
            phone = f"202-555-01{i:02d}"
            r = c.post("/api/leads", json=valid_lead(phone=phone, email=f"lead{i}@example.com"))
            assert r.status_code == 201
            assert r.json()["scoring"]["scored_by"] is None  # capture did not wait
        _run_concurrently(app, n_workers)

        with app.state.session_factory() as s:
            statuses = Counter(s.scalars(select(Job.status)))
            # Each lead: 1 score + 2 instant acks + 1 CRM sync done; 2 later steps queued.
            assert statuses == {"done": 4 * n_leads, "queued": 2 * n_leads}
            sent = Counter(s.scalars(select(OutboxMessage.step)))
            assert sent == {"ack_sms": n_leads, "ack_email": n_leads}  # exactly once each
            assert s.scalar(select(func.count()).where(Lead.scored_at.is_not(None))) == n_leads
            scored = s.scalar(select(func.count()).where(Activity.kind == "ai_scored"))
            assert scored == n_leads  # no job ran twice
            crm = app.state.job_context.crm
            assert len(crm.contacts) == n_leads
            assert len(crm.deals) == n_leads
    return app


def test_end_to_end_sqlite_file_with_concurrent_workers(tmp_path: Path) -> None:
    _capture_and_drain(f"sqlite:///{tmp_path / 'e2e.db'}", n_leads=8, n_workers=4)


@pytest.mark.skipif(not PG_URL, reason="set TEST_POSTGRES_URL to run against Postgres")
def test_end_to_end_postgres_skip_locked() -> None:
    assert PG_URL is not None
    app = create_app(make_settings(database_url=PG_URL))
    from app.db import Base

    Base.metadata.drop_all(app.state.engine)
    try:
        _capture_and_drain(PG_URL, n_leads=10, n_workers=5)
    finally:
        Base.metadata.drop_all(app.state.engine)
        app.state.engine.dispose()


def test_main_drain_runs_jobs(tmp_path: Path) -> None:
    settings = make_settings(database_url=f"sqlite:///{tmp_path / 'd.db'}")
    app = create_app(settings)
    with TestClient(app) as c:
        assert c.post("/api/leads", json=valid_lead()).status_code == 201
    assert main(["--drain"], settings=settings) == 0
    with app.state.session_factory() as s:
        assert s.scalar(select(func.count()).select_from(OutboxMessage)) == 2


def test_main_with_real_provider_builds_and_closes_client(tmp_path: Path) -> None:
    from pydantic import SecretStr

    settings = make_settings(
        database_url=f"sqlite:///{tmp_path / 'p.db'}",
        ai_provider="claude",
        anthropic_api_key=SecretStr("k-test"),
    )
    with TestClient(create_app(settings)):
        pass  # creates the schema
    assert main(["--drain"], settings=settings) == 0  # nothing queued: no network call


def test_main_runs_until_signalled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_settings(
        database_url=f"sqlite:///{tmp_path / 'f.db'}", worker_poll_seconds=0.01
    )
    with TestClient(create_app(settings)):
        pass
    handlers: dict[int, Any] = {}
    monkeypatch.setattr("app.worker.signal.signal", lambda sig, fn: handlers.setdefault(sig, fn))
    original = Worker.run_forever

    def run_briefly(self: Worker, stop: threading.Event, poll: float = 1.0) -> None:
        threading.Timer(0.05, lambda: next(iter(handlers.values()))(15, None)).start()
        original(self, stop, poll)

    monkeypatch.setattr(Worker, "run_forever", run_briefly)
    assert main([], settings=settings) == 0
    assert len(handlers) == 2  # SIGTERM and SIGINT


def test_run_forever_survives_loop_errors(client: TestClient) -> None:
    worker = make_worker(client.app)
    stop = threading.Event()
    calls = {"n": 0}

    def flaky() -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database went away")
        if calls["n"] >= 3:
            stop.set()
        return False

    worker.run_once = flaky  # type: ignore[method-assign]
    worker.run_forever(stop, poll_seconds=0.001)
    assert calls["n"] >= 3


def test_recover_runs_on_an_interval(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    monkeypatch.setattr(queue, "recover_expired", lambda sf, now: calls.append(now) or 0)
    worker = make_worker(client.app)
    worker.run_once()
    worker.run_once()
    assert len(calls) == 1  # second call is within recover_every


@pytest.mark.skipif(not PG_URL, reason="set TEST_POSTGRES_URL to run against Postgres")
def test_postgres_claim_skips_a_row_locked_by_another_worker() -> None:
    from datetime import UTC, datetime

    from app.db import Base, make_engine, make_session_factory

    assert PG_URL is not None
    engine = make_engine(PG_URL)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    sf = make_session_factory(engine)
    now = datetime.now(UTC)
    try:
        with sf() as s:
            queue.enqueue(s, "demo", {}, idempotency_key="first", now=now)
            queue.enqueue(s, "demo", {}, idempotency_key="second", now=now)
            s.commit()
        with sf() as holder:  # another worker is mid-claim on "first" and holds its row lock
            holder.execute(
                select(Job).where(Job.idempotency_key == "first").with_for_update()
            ).one()
            job = queue.claim_next(sf, "w2", now=now, lease_seconds=30)  # must not block
            assert job is not None
            assert job.idempotency_key == "second"
            assert queue.claim_next(sf, "w3", now=now, lease_seconds=30) is None
            holder.rollback()
        job = queue.claim_next(sf, "w3", now=now, lease_seconds=30)
        assert job is not None
        assert job.idempotency_key == "first"
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
