"""Data retention: anonymize (default) or delete leads older than RETENTION_DAYS."""

import io
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import cli
from app.db import init_db, make_engine, make_session_factory
from app.models import Activity, AuditEvent, Job, Lead, OutboxMessage, Task, UserSession, utcnow
from app.services.handoff import LEAD_REPLIED, handoff
from app.services.retention import ANONYMIZED_NAME, run_retention
from tests.conftest import FixedClock, add_user, drain, make_settings, seed_lead
from tests.web_helpers import app_of


def _old_and_new(client: TestClient) -> tuple[object, object]:
    app = app_of(client)
    clock = FixedClock(utcnow() - timedelta(days=400))
    old = seed_lead(app, clock, message="Call me at (202) 555-0123 about the roof")
    new = seed_lead(app, phone="(202) 555-0199", email="recent@example.com")
    drain(client, clock)
    drain(client)
    with app.state.session_factory() as s:
        lead = s.get(Lead, old)
        assert lead is not None
        handoff(s, lead, LEAD_REPLIED, now=clock(), detail="my number is 202-555-0123")
        s.add(
            Activity(
                lead_id=old,
                kind="inbound_reply",
                actor="lead",
                detail={"channel": "sms", "body": "text me at 202-555-0123"},
                created_at=clock(),
            )
        )
        s.commit()
    return old, new


def test_anonymize_keeps_metrics_rows_and_removes_personal_data(client: TestClient) -> None:
    old, new = _old_and_new(client)
    app = app_of(client)
    with app.state.session_factory() as s:
        report = run_retention(s, now=utcnow(), days=365)
        s.commit()
        assert (report.leads, report.mode) == (1, "anonymize")
        assert report.outbox_removed >= 1
        lead = s.get(Lead, old)
        assert lead is not None
        assert lead.name == ANONYMIZED_NAME
        assert (lead.phone, lead.email, lead.message) == (None, None, None)
        assert (lead.phone_bidx, lead.email_bidx) == (None, None)
        assert lead.anonymized_at is not None
        assert lead.score is not None  # metrics keep working
        assert (
            s.scalar(select(func.count(OutboxMessage.id)).where(OutboxMessage.lead_id == old)) == 0
        )
        assert s.scalar(select(Task.detail).where(Task.lead_id == old)) is None
        details = [a.detail for a in s.scalars(select(Activity).where(Activity.lead_id == old))]
        assert "555-0123" not in str(details)
        assert any(a == {"channel": "sms"} for a in details)
        kinds = set(s.scalars(select(Activity.kind).where(Activity.lead_id == old)))
        assert "lead_anonymized" in kinds
        # The recent lead is untouched.
        recent = s.get(Lead, new)
        assert recent is not None
        assert recent.email == "recent@example.com"
        assert s.scalar(select(func.count(Lead.id)).where(Lead.phone_is("+12025550123"))) == 0
        # Running again changes nothing.
        assert run_retention(s, now=utcnow(), days=365).leads == 0
        s.commit()
        runs = list(s.scalars(select(AuditEvent).where(AuditEvent.action == "retention_run")))
    assert [r.summary["leads"] for r in runs] == [1, 0]


def test_delete_mode_removes_the_lead_and_its_rows(client: TestClient) -> None:
    old, new = _old_and_new(client)
    with app_of(client).state.session_factory() as s:
        report = run_retention(s, now=utcnow(), days=365, mode="delete")
        s.commit()
        assert report.leads == 1
        assert s.get(Lead, old) is None
        for model in (Activity, Job, OutboxMessage, Task):
            assert (
                s.scalar(select(func.count()).select_from(model).where(model.lead_id == old)) == 0
            )
        assert s.get(Lead, new) is not None


def test_dry_run_changes_nothing(client: TestClient) -> None:
    old, _ = _old_and_new(client)
    with app_of(client).state.session_factory() as s:
        report = run_retention(s, now=utcnow(), days=365, dry_run=True)
        s.commit()
        assert report.dry_run
        assert report.leads == 1
        lead = s.get(Lead, old)
        assert lead is not None
        assert lead.anonymized_at is None
        assert s.scalar(select(func.count(AuditEvent.id))) == 0


def test_old_login_sessions_are_purged(client: TestClient) -> None:
    app = app_of(client)
    user_id = add_user(app, "rep")
    now = utcnow()
    with app.state.session_factory() as s:
        for i, (expires, revoked) in enumerate(
            [
                (now - timedelta(days=3), None),  # long expired
                (now + timedelta(hours=1), now - timedelta(days=2)),  # revoked long ago
                (now + timedelta(hours=1), None),  # live
            ]
        ):
            s.add(
                UserSession(
                    token_hash=f"h{i}",
                    user_id=user_id,
                    expires_at=expires,
                    revoked_at=revoked,
                )
            )
        s.commit()
        assert run_retention(s, now=now, days=365).sessions_removed == 2
        s.commit()
        assert [r.token_hash for r in s.scalars(select(UserSession))] == ["h2"]


def test_retention_cli(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'r.db'}"
    engine = make_engine(url)
    init_db(engine)
    with make_session_factory(engine)() as s:
        s.add(
            Lead(
                name="Old Lead",
                phone="+12025550111",
                source="api",
                created_at=utcnow() - timedelta(days=40),
            )
        )
        s.commit()
    settings = make_settings(database_url=url, retention_days=30)
    out = io.StringIO()
    assert cli.main(["retention", "--dry-run"], settings=settings, out=out) == 0
    assert "would anonymize 1 leads" in out.getvalue()
    out = io.StringIO()
    assert cli.main(["retention"], settings=settings, out=out) == 0
    assert "anonymized 1 leads" in out.getvalue()
    with make_session_factory(engine)() as s:
        assert s.scalars(select(Lead)).one().name == ANONYMIZED_NAME
    delete = make_settings(database_url=url, retention_days=30, retention_mode="delete")
    out = io.StringIO()
    assert cli.main(["retention"], settings=delete, out=out) == 0
    assert "deleted 0 leads" in out.getvalue()  # already anonymized ones are left alone
    engine.dispose()


def test_anonymized_lead_page_says_so() -> None:
    from app.models import UserRole
    from tests.web_helpers import login_as, make_client

    for c in make_client():
        old, _ = _old_and_new(c)
        with app_of(c).state.session_factory() as s:
            run_retention(s, now=utcnow(), days=365)
            s.commit()
        login_as(c, UserRole.ADMIN)
        page = c.get(f"/dashboard/leads/{old}").text
        assert "Personal data removed" in page
        assert "Show contact details" not in page
