"""Demo mode: deterministic seeding, the simulator through real endpoints, DEMO_MODE runtime."""

import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import DEFAULT_DEMO_PASSWORD
from app.db import make_engine, make_session_factory
from app.demo import __main__ as demo_main
from app.demo.runtime import DemoRunner, demo_base_url, reseed
from app.demo.seed import FIRST_NAMES, reset_database, seed_demo
from app.demo.simulator import Simulator
from app.followups.guards import DEFAULT_SEND_WINDOWS, SendWindow
from app.main import create_app
from app.models import Activity, Job, Lead, OutboxMessage, Task, User
from app.services import metrics as m
from tests.conftest import ADMIN, ADMIN_TOKEN, INBOUND_SECRET, META_SECRET, drain, make_settings

WINDOW = SendWindow.parse(DEFAULT_SEND_WINDOWS)
# The fixed-seed snapshot: change it only on purpose (it is what the demo looks like).
EXPECTED_STAGES = {"new": 32, "qualified": 21, "contacted": 36, "won": 28, "lost": 33}


@pytest.fixture
def sf() -> Iterator[sessionmaker[Session]]:
    engine = make_engine("sqlite://")
    reset_database(engine)  # the real migrations
    yield make_session_factory(engine)
    engine.dispose()


def _seed(sf: sessionmaker[Session], now: datetime, seed: int = 4242, count: int = 150) -> Any:
    return seed_demo(
        sf,
        seed=seed,
        count=count,
        now=now,
        business_timezone="America/New_York",
        window=WINDOW,
        company="Copperline Roofing & Solar (fictional)",
    )


def _stage_counts(sf: sessionmaker[Session]) -> dict[str, int]:
    with sf() as s:
        rows = s.execute(select(Lead.stage, func.count()).group_by(Lead.stage)).all()
        return {str(stage): int(n) for stage, n in rows}


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 3, 2, 15, 0, tzinfo=UTC),  # Monday morning in New York
        datetime(2026, 7, 5, 3, 30, tzinfo=UTC),  # Saturday night, summer time
        datetime(2026, 11, 1, 18, 0, tzinfo=UTC),  # the day DST ends
    ],
)
def test_seed_is_deterministic(sf: sessionmaker[Session], now: datetime) -> None:
    report = _seed(sf, now)
    assert report.leads == 150
    assert report.stages == EXPECTED_STAGES  # same seed -> same pipeline, any clock time
    assert _stage_counts(sf) == EXPECTED_STAGES
    assert report.dead_jobs == 2
    with sf() as s:
        assert s.scalar(select(func.count()).select_from(Job).where(Job.status == "dead")) == 2
        assert s.scalar(select(func.max(Lead.created_at))) < now  # nothing in the future
        assert s.scalar(select(func.max(Activity.created_at))) < now
        assert s.scalar(select(func.max(OutboxMessage.sent_at))) < now
        assert s.scalar(select(func.count()).select_from(Task).where(Task.status == "open")) > 0
        assert s.scalar(select(func.count(Lead.id)).where(Lead.opted_out_at.is_not(None))) > 0
        sources = dict(s.execute(select(Lead.source, func.count()).group_by(Lead.source)).all())
        assert set(sources) == {"web_form", "meta_ads", "google_ads", "referral"}
        leads = list(s.scalars(select(Lead)))
        assert all(lead.email is None or lead.email.endswith("@example.com") for lead in leads)
        assert all(lead.phone is None or lead.phone[5:9] == "5550" for lead in leads)
        assert all(lead.phone is None or "55501" in lead.phone for lead in leads)
        assert all(lead.name.split()[0] in FIRST_NAMES for lead in leads)
        # The metrics read the seeded history sensibly.
        period = m.Period.last_days(now, 30)
        assert m.median_response_seconds(s, period) is not None
        steps = m.funnel(s, period)
        assert steps[0].reached >= steps[1].reached >= steps[2].reached >= steps[3].reached
        assert all(lead.score is not None for lead in leads)


def test_same_seed_same_people_different_seed_different(sf: sessionmaker[Session]) -> None:
    now = datetime(2026, 3, 2, 15, tzinfo=UTC)
    _seed(sf, now, count=20)
    with sf() as s:
        first = sorted(s.scalars(select(Lead.name)))
    reset_database(sf.kw["bind"])
    _seed(sf, now, count=20)
    with sf() as s:
        assert sorted(s.scalars(select(Lead.name))) == first
    reset_database(sf.kw["bind"])
    _seed(sf, now, seed=99, count=20)
    with sf() as s:
        assert sorted(s.scalars(select(Lead.name))) != first


def test_after_hours_seeded_leads_use_the_after_hours_wording(sf: sessionmaker[Session]) -> None:
    _seed(sf, datetime(2026, 3, 2, 15, tzinfo=UTC))
    with sf() as s:
        acks = list(s.scalars(select(OutboxMessage).where(OutboxMessage.step == "ack_sms")))
    after_hours = [a for a in acks if "within 15 minutes" not in a.body]
    assert after_hours
    assert all("will call you this" in a.body for a in after_hours)


def test_demo_mode_refuses_postgres_and_fills_secrets() -> None:
    with pytest.raises(ValidationError, match="only runs on SQLite"):
        make_settings(demo_mode=True, database_url="postgresql+psycopg://u@db/x")
    with pytest.raises(ValidationError, match="DEMO_INTERVAL"):
        make_settings(demo_mode=True, demo_interval_min_seconds=9, demo_interval_max_seconds=2)
    s = make_settings(demo_mode=True, admin_token=None, meta_app_secret=None)
    assert s.admin_token is not None
    assert len(s.admin_token.get_secret_value()) > 20  # random, per process
    assert s.meta_app_secret is not None
    assert s.demo_password is not None
    assert s.demo_password.get_secret_value() == DEFAULT_DEMO_PASSWORD
    assert s.demo_role_list == ["admin", "rep", "viewer"]
    assert s.login_lockout_max_seconds <= 60  # shared public accounts: short lockouts only
    assert demo_base_url(s) == "http://127.0.0.1:8000"
    assert make_settings().demo_password is None  # outside demo mode there is no demo login
    assert make_settings(demo_mode=True, demo_roles="viewer, nobody").demo_role_list == ["viewer"]
    with pytest.raises(ValidationError, match="DEMO_ROLES"):
        make_settings(demo_mode=True, demo_roles="superuser")


def test_simulator_posts_through_the_real_endpoints(client: TestClient) -> None:
    sim = Simulator(
        client,
        meta_secret=META_SECRET,
        inbound_secret=INBOUND_SECRET,
        admin_token=ADMIN_TOKEN,
        seed=3,
    )
    person = sim._person()
    assert person["email"].endswith("@example.com")
    assert sim.post_meta_lead(person).json()["created"] == 1  # signed webhook accepted
    assert sim.post_api_lead(sim._person(), "google_ads").status_code == 201
    outcomes = [sim.new_lead() for _ in range(6)]
    assert all(o.split(":")[2] in ("200", "201") for o in outcomes)
    drain(client)
    assert sim.advance_stage().startswith("stage:")
    assert sim.reply().startswith("reply:200")
    for _ in range(10):
        assert sim.tick() != "error"
    app: FastAPI = client.app  # type: ignore[assignment]
    with app.state.session_factory() as s:
        sources = set(s.scalars(select(Lead.source)))
        assert "meta_ads" in sources
        assert "google_ads" in sources
        events = set(s.scalars(select(Activity.kind)))
    assert {"lead_created", "ai_scored", "followup_sent"} <= events
    assert (
        Simulator(client, meta_secret="x", inbound_secret="y", admin_token="z").reply()
        == "reply:none"
    )


def test_simulator_run_loop_stops(client: TestClient) -> None:
    sim = Simulator(
        client, meta_secret=META_SECRET, inbound_secret=INBOUND_SECRET, admin_token=ADMIN_TOKEN
    )
    stop = threading.Event()
    thread = threading.Thread(
        target=sim.run, args=(stop,), kwargs={"min_seconds": 0.01, "max_seconds": 0.02}
    )
    thread.start()
    time.sleep(0.3)
    stop.set()
    thread.join(5)
    assert not thread.is_alive()
    assert sim.counter >= 1


def test_simulator_survives_http_errors() -> None:
    import httpx

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    with httpx.Client(transport=httpx.MockTransport(boom), base_url="http://x") as c:
        sim = Simulator(c, meta_secret="a", inbound_secret="b", admin_token="c")
        assert {sim.tick() for _ in range(5)} == {"error"}


def test_demo_mode_app_reseeds_and_runs_the_worker(tmp_path: Any) -> None:
    # A real file: the worker thread and the requests need their own connections.
    settings = make_settings(
        demo_mode=True,
        demo_leads=12,
        worker_poll_seconds=0.05,
        database_url=f"sqlite:///{tmp_path / 'demo.db'}",
    )
    app = create_app(settings, demo_simulate=False)
    with TestClient(app, base_url="http://localhost", follow_redirects=False) as c:
        page = c.get("/login")
        assert "Demo accounts" in page.text
        assert DEFAULT_DEMO_PASSWORD in page.text
        for email in ("admin@example.com", "rep@example.com", "viewer@example.com"):
            assert email in page.text
        with app.state.session_factory() as s:
            assert s.scalar(select(func.count(Lead.id))) == 12
            rep_id = s.scalar(select(User.id).where(User.email == "rep@example.com"))
            assert s.scalar(select(func.count(Lead.id)).where(Lead.assignee_id == rep_id)) == 4
        csrf = c.cookies.get("stl_csrf")
        r = c.post(
            "/login",
            data={
                "email": "viewer@example.com",
                "password": DEFAULT_DEMO_PASSWORD,
                "csrf_token": csrf,
            },
        )
        assert r.status_code == 303
        assert "viewer" in c.get("/dashboard").text
        # The demo password never works on the operator API.
        demo_bearer = {"Authorization": f"Bearer {DEFAULT_DEMO_PASSWORD}"}
        assert c.get("/admin/jobs", headers=demo_bearer).status_code == 401
        # The in-process worker picks up a new lead without anyone draining the queue.
        r = c.post(
            "/api/leads",
            json={"name": "Robin Vale", "phone": "(512) 555-0142", "source": "web_form"},
        )
        lead_id = r.json()["id"]
        deadline = time.monotonic() + 5
        scored = False
        while time.monotonic() < deadline and not scored:
            detail = c.get(
                f"/api/leads/{lead_id}",
                headers={"Authorization": f"Bearer {settings.admin_token.get_secret_value()}"},
            )  # type: ignore[union-attr]
            scored = detail.json()["scoring"]["score"] is not None
            time.sleep(0.05)
        assert scored


def test_reseed_helper_and_seed_only_cli(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    url = f"sqlite:///{tmp_path / 'demo.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("DEMO_LEADS", "5")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    monkeypatch.setenv("DEMO_MODE", "1")  # main() sets these; monkeypatch restores them
    monkeypatch.setenv("PORT", "8000")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        assert demo_main.main(["--seed-only"]) == 0
    finally:
        get_settings.cache_clear()
    assert "seeded 5 leads" in capsys.readouterr().out
    engine = make_engine(url)
    report = reseed(
        make_settings(demo_mode=True, database_url=url, demo_leads=4),
        engine,
        make_session_factory(engine),
    )
    assert report.leads == 4
    engine.dispose()


def test_runner_shutdown_joins_threads(tmp_path: Any) -> None:
    url = f"sqlite:///{tmp_path / 'demo.db'}"
    app = create_app(
        make_settings(demo_mode=True, demo_leads=0, database_url=url), demo_simulate=False
    )
    runner = DemoRunner(
        app.state.settings, app.state.session_factory, app.state.job_context, simulate=False
    )
    runner.start()
    runner.shutdown()
    assert all(not t.is_alive() for t in runner.threads)


def test_bearer_admin_still_reads_metrics(client: TestClient) -> None:
    assert client.get("/api/metrics/kpis", headers=ADMIN).status_code == 200


def test_demo_first_names_come_from_the_fixed_list() -> None:
    assert len(set(FIRST_NAMES)) == len(FIRST_NAMES) >= 50


def test_demo_cli_prints_the_demo_accounts(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    import uvicorn

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'demo.db'}")
    monkeypatch.setenv("DEMO_MODE", "1")
    monkeypatch.setenv("PORT", "8000")
    calls: list[Any] = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: calls.append((a, kw)))
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        assert demo_main.main([]) == 0
    finally:
        get_settings.cache_clear()
    out = capsys.readouterr().out
    assert "admin@example.com, rep@example.com, viewer@example.com / copperline-demo" in out
    assert calls
