"""Interview mode end to end: invites, the /try page, the live panel, and the rule that the
real email sender is used only for an invite's one email (and only when turned on)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import timedelta
from email.message import EmailMessage
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select, text, update

from app.followups.senders import MockEmailSender, MockSmsSender, OutboundMessage
from app.followups.sequence import Channel
from app.interview import INTERVIEW_SOURCE, invites
from app.interview.mailer import FOOTER, SUBJECT, EmailDeliveryError, SmtpBackend
from app.interview.routing import EmailCaps, InterviewEmailRouter
from app.logging import configure_logging
from app.main import create_app
from app.models import (
    AuditEvent,
    InterviewInvite,
    Job,
    JobStatus,
    Lead,
    OutboxMessage,
    UserRole,
    utcnow,
)
from tests.conftest import FixedClock, drain, make_settings, make_worker, valid_lead
from tests.web_helpers import app_of, csrf, login_as, post

VISITOR = "visitor@example.com"
REAL = {
    "email_backend": "smtp",
    "interview_email_enabled": True,
    "email_from": "sender@example.com",
    "smtp_host": "smtp.example.com",
    "smtp_username": "sender@example.com",
    "smtp_password": SecretStr("app-password"),  # pragma: allowlist secret
}


class FakeBackend:
    """Stands in for SMTP: records what would have been delivered."""

    name = "SMTP-email"

    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []
        self.fail = False

    def deliver(self, message: EmailMessage) -> None:
        if self.fail:
            raise EmailDeliveryError("SMTP error: SMTPRecipientsRefused")
        self.sent.append(message)


def _client(backend: Any = None, **overrides: Any) -> Iterator[TestClient]:
    app = create_app(make_settings(**overrides), email_backend=backend)
    with TestClient(app, base_url="http://localhost", follow_redirects=False) as c:
        yield c


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def real(backend: FakeBackend) -> Iterator[TestClient]:
    """Real email turned on (the backend is a fake: no network in tests)."""
    yield from _client(backend, **REAL)


@pytest.fixture
def mock_only(backend: FakeBackend) -> Iterator[TestClient]:
    """Default settings: EMAIL_BACKEND=mock. The fake backend must never be used."""
    yield from _client(backend)


def visitor(c: TestClient) -> TestClient:
    """A second browser on the same app (no dashboard login)."""
    return TestClient(app_of(c), base_url="http://localhost", follow_redirects=False)


def create_invite(c: TestClient, label: str = "Acme interview") -> str:
    """Log in as an admin (once), create an invite on the dashboard, return its token."""
    if "Interview mode" not in c.get("/dashboard").text:
        login_as(c, UserRole.ADMIN)
    r = post(c, "/dashboard/interview", {"label": label}, csrf_page="/dashboard/interview")
    assert r.status_code == 201, r.text
    match = re.search(r"http://localhost/try/([A-Za-z0-9_\-]+)", r.text)
    assert match, r.text
    return match.group(1)


def submit(v: TestClient, token: str, **fields: str) -> Any:
    page = v.get(f"/try/{token}")
    assert page.status_code == 200, page.text
    data = {
        "first_name": "Avery",
        "email": VISITOR,
        "message": "Curious how fast this is.",
        "consent": "yes",
        "csrf_token": v.cookies.get("stl_csrf") or "",
    }
    data.update(fields)
    return v.post(f"/try/{token}", data=data)


def use_invite(c: TestClient, email: str = VISITOR, label: str = "Acme") -> tuple[TestClient, str]:
    token = create_invite(c, label)
    v = visitor(c)
    r = submit(v, token, email=email)
    assert r.status_code == 303, r.text
    assert r.headers["location"] == f"/try/{token}"
    return v, token


def lead_of(c: TestClient, token: str) -> Lead:
    with app_of(c).state.session_factory() as s:
        invite = invites.find_by_token(s, token)
        assert invite is not None
        assert invite.lead_id is not None
        lead = s.get(Lead, invite.lead_id)
        assert lead is not None
        s.expunge(lead)
        return lead


def outbox(c: TestClient) -> list[tuple[str, str, str]]:
    with app_of(c).state.session_factory() as s:
        rows = s.scalars(select(OutboxMessage).order_by(OutboxMessage.id))
        return [(m.step, m.channel, m.sender) for m in rows]


def invite_row(c: TestClient, token: str) -> InterviewInvite:
    with app_of(c).state.session_factory() as s:
        invite = invites.find_by_token(s, token)
        assert invite is not None
        s.expunge(invite)
        return invite


# --------------------------------------------------------------------------- invites


def test_create_shows_the_link_once_and_stores_only_a_hash(mock_only: TestClient) -> None:
    c = mock_only
    token = create_invite(c, "Acme interview")
    assert len(token) >= 40
    page = c.get("/dashboard/interview").text
    assert token not in page  # never shown again
    assert "Acme interview" in page
    assert "active" in page
    with app_of(c).state.session_factory() as s:
        row = s.scalar(select(InterviewInvite))
        assert row is not None
        assert row.token_hash == invites.hash_token(token)
        assert row.token_hash != token
        assert row.created_by == "admin@example.com"
        assert row.expires_at - row.created_at == timedelta(minutes=120)
        dump = " ".join(str(v) for v in s.execute(text("SELECT * FROM interview_invites")).one())
        assert token not in dump
        event = s.scalar(select(AuditEvent).where(AuditEvent.action == "invite_created"))
        assert event is not None
        assert event.summary == {"label": "Acme interview", "ttl_minutes": 120}
        assert token not in str(event.summary)


def test_invite_ttl_is_configurable() -> None:
    for c in _client(invite_ttl_minutes=30):
        token = create_invite(c)
        row = invite_row(c, token)
        assert row.expires_at - row.created_at == timedelta(minutes=30)


def test_rep_can_create_viewer_cannot(mock_only: TestClient) -> None:
    c = mock_only
    login_as(c, UserRole.VIEWER)
    assert "Interview mode" not in c.get("/dashboard").text
    assert c.get("/dashboard/interview").status_code == 403
    r = post(c, "/dashboard/interview", {"label": "x"})
    assert r.status_code == 403
    c.post("/logout", data={"csrf_token": csrf(c)})
    login_as(c, UserRole.REP)
    assert "Interview mode" in c.get("/dashboard").text
    r = post(c, "/dashboard/interview", {"label": "rep"}, csrf_page="/dashboard/interview")
    assert r.status_code == 201


def test_create_needs_login_and_csrf(mock_only: TestClient) -> None:
    c = mock_only
    r = c.post("/dashboard/interview", data={"label": "x"})
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")
    login_as(c, UserRole.ADMIN)
    assert c.post("/dashboard/interview", data={"label": "x"}).status_code == 403
    assert (
        c.post("/dashboard/interview", data={"label": "x", "csrf_token": "bad"}).status_code == 403
    )
    with app_of(c).state.session_factory() as s:
        assert s.scalar(select(InterviewInvite)) is None


def test_revoke(mock_only: TestClient) -> None:
    c = mock_only
    token = create_invite(c)
    invite_id = invite_row(c, token).id
    r = post(c, f"/dashboard/interview/{invite_id}/revoke", csrf_page="/dashboard/interview")
    assert r.status_code == 303
    assert "Link revoked" in c.get(r.headers["location"]).text
    assert invite_row(c, token).status(utcnow()) == "revoked"
    v = visitor(c)
    assert v.get(f"/try/{token}").status_code == 404
    # Revoking again (or an unknown id) is recorded as failed and changes nothing.
    post(c, f"/dashboard/interview/{invite_id}/revoke", csrf_page="/dashboard/interview")
    post(c, "/dashboard/interview/9999/revoke", csrf_page="/dashboard/interview")
    with app_of(c).state.session_factory() as s:
        outcomes = list(
            s.scalars(
                select(AuditEvent.outcome)
                .where(AuditEvent.action == "invite_revoked")
                .order_by(AuditEvent.id)
            )
        )
    assert outcomes == ["ok", "failed", "failed"]


def test_expired_invite_is_closed(mock_only: TestClient) -> None:
    c = mock_only
    token = create_invite(c)
    v = visitor(c)
    assert v.get(f"/try/{token}").status_code == 200
    with app_of(c).state.session_factory() as s:
        s.execute(update(InterviewInvite).values(expires_at=utcnow() - timedelta(seconds=1)))
        s.commit()
    page = v.get(f"/try/{token}")
    assert page.status_code == 404
    assert "not valid, has expired, or was already used" in page.text
    r = v.post(
        f"/try/{token}",
        data={
            "first_name": "A",
            "email": VISITOR,
            "consent": "yes",
            "csrf_token": v.cookies.get("stl_csrf"),
        },
    )
    assert r.status_code == 404
    assert "expired" in c.get("/dashboard/interview").text


def test_unknown_or_malformed_tokens_are_closed(mock_only: TestClient) -> None:
    v = visitor(mock_only)
    for token in ("nope", "x" * 43, "a" * 200, "bad%20token%20with%20spaces%20in%20it%20ok"):
        r = v.get(f"/try/{token}")
        assert r.status_code == 404, token
        assert r.headers["x-robots-tag"] == "noindex, nofollow"
    assert v.get("/try/nope/status").status_code == 404


def test_single_use(mock_only: TestClient) -> None:
    c = mock_only
    v, token = use_invite(c)
    # The same browser sees its panel; another one sees a closed link.
    assert "what happened to your lead" in v.get(f"/try/{token}").text
    other = visitor(c)
    assert other.get(f"/try/{token}").status_code == 404
    # A second submission (even replaying the form) creates nothing.
    r = v.post(
        f"/try/{token}",
        data={
            "first_name": "Bo",
            "email": "second@example.com",
            "consent": "yes",
            "csrf_token": v.cookies.get("stl_csrf"),
        },
    )
    assert r.status_code == 404
    with app_of(c).state.session_factory() as s:
        assert len(list(s.scalars(select(Lead).where(Lead.source == INTERVIEW_SOURCE)))) == 1
        used = s.scalars(select(AuditEvent).where(AuditEvent.action == "invite_used")).all()
    assert len(used) == 1
    assert used[0].actor == "interview-link"
    assert VISITOR not in str(used[0].summary)


def test_claim_is_atomic(mock_only: TestClient) -> None:
    c = mock_only
    token = create_invite(c)
    with app_of(c).state.session_factory() as s:
        invite = invites.find_by_token(s, token)
        assert invite is not None
        now = utcnow()
        assert invites.claim(s, invite, now=now)
        assert not invites.claim(s, invite, now=now)  # the second claimant loses
        assert not invites.revoke(s, invite, now=now)  # used invites cannot be revoked


# --------------------------------------------------------------------------- /try form


def test_try_page_explains_the_demo(mock_only: TestClient) -> None:
    token = create_invite(mock_only)
    page = visitor(mock_only).get(f"/try/{token}")
    assert page.status_code == 200
    body = page.text
    assert "You're seeing a live demo" in body
    assert "Enter your own email to receive the real speed-to-lead reply" in body
    assert (
        "I agree to receive one demo email at this address. This is a one-time demo; "
        "no marketing." in body
    )
    consent = re.search(r'<input type="checkbox" name="consent"[^>]*>', body)
    assert consent
    assert "checked" not in consent.group(0)
    assert 'name="first_name"' in body
    assert re.search(r'id="first_name"[^>]*value=""', body)  # empty by default
    assert 'name="website"' in body  # honeypot
    assert 'name="robots" content="noindex, nofollow"' in body
    assert page.headers["x-robots-tag"] == "noindex, nofollow"
    assert "Demo." in body


def test_consent_is_required(mock_only: TestClient) -> None:
    c = mock_only
    token = create_invite(c)
    v = visitor(c)
    r = submit(v, token, consent="")
    assert r.status_code == 422
    assert "agree to receive the one demo email" in r.text
    assert invite_row(c, token).used_at is None  # still usable


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"email": ""}, "Enter your email address"),
        ({"email": "not-an-email"}, "email"),
        ({"email": "a" * 250 + "@example.com"}, "at most 254"),
        ({"first_name": ""}, "Enter your first name"),
        ({"first_name": "A" * 41}, "at most 40"),
        ({"first_name": "http://spam.example"}, "links"),
        ({"message": "x" * 501}, "at most 500"),
    ],
)
def test_validation(mock_only: TestClient, fields: dict[str, str], message: str) -> None:
    c = mock_only
    token = create_invite(c)
    r = submit(visitor(c), token, **fields)
    assert r.status_code == 422
    assert message in r.text
    assert invite_row(c, token).used_at is None


def test_csrf_is_required(mock_only: TestClient) -> None:
    c = mock_only
    token = create_invite(c)
    r = submit(visitor(c), token, csrf_token="forged")
    assert r.status_code == 403
    assert invite_row(c, token).used_at is None


def test_honeypot_looks_fine_but_does_nothing(mock_only: TestClient) -> None:
    c = mock_only
    token = create_invite(c)
    r = submit(visitor(c), token, website="http://spam.example")
    assert r.status_code == 303
    assert r.headers["location"] == "/thanks"
    assert invite_row(c, token).used_at is None
    with app_of(c).state.session_factory() as s:
        assert s.scalar(select(Lead).where(Lead.source == INTERVIEW_SOURCE)) is None


def test_rate_limit_per_ip() -> None:
    for c in _client(interview_form_per_ip_per_hour=2):
        token = create_invite(c)
        v = visitor(c)
        for _ in range(2):
            assert submit(v, token, consent="").status_code == 422
        r = submit(v, token)
        assert r.status_code == 429
        assert "Too many attempts" in r.text
        assert invite_row(c, token).used_at is None


def test_the_reserved_source_is_refused_on_the_public_api(mock_only: TestClient) -> None:
    r = mock_only.post("/api/leads", json={**valid_lead(), "source": INTERVIEW_SOURCE})
    assert r.status_code == 422


# --------------------------------------------------------------------------- real vs mock


def test_invite_lead_gets_the_one_real_email(real: TestClient, backend: FakeBackend) -> None:
    c = real
    _, token = use_invite(c)
    drain(c)
    assert len(backend.sent) == 1
    msg = backend.sent[0]
    assert msg["To"] == VISITOR
    assert msg["Subject"] == SUBJECT
    plain = msg.get_body(("plain",))
    assert plain is not None
    assert FOOTER in plain.get_content()
    assert ("ack_email", "email", "SMTP-email") in outbox(c)
    row = invite_row(c, token)
    assert row.email_result == "sent"
    assert row.email_sent_at is not None
    # The lead went through the normal pipeline: scored and synced to the mock CRM.
    lead = lead_of(c, token)
    assert lead.source == INTERVIEW_SOURCE
    assert lead.scored_at is not None
    assert lead.crm_synced_at is not None
    assert lead.first_response_at is not None


def test_public_form_and_api_never_use_the_real_sender(
    real: TestClient, backend: FakeBackend
) -> None:
    c = real
    token = c.get("/").cookies.get("stl_csrf") or c.cookies.get("stl_csrf")
    r = c.post(
        "/",
        data={
            "name": "Jordan Alvarez",
            "email": VISITOR,
            "phone": "(202) 555-0123",
            "csrf_token": token,
        },
    )
    assert r.status_code == 303
    assert c.post("/api/leads", json=valid_lead(email="api@example.com")).status_code == 201
    drain(c)
    assert backend.sent == []
    senders = {sender for _, _, sender in outbox(c)}
    assert senders == {"MOCK-sms", "MOCK-email"}


def test_a_lead_with_the_source_but_no_invite_stays_mock(
    real: TestClient, backend: FakeBackend
) -> None:
    """Even a lead marked interview_demo (e.g. by an operator script) needs its invite row."""
    from tests.conftest import seed_lead

    seed_lead(app_of(real), source=INTERVIEW_SOURCE)
    drain(real)
    assert backend.sent == []
    assert ("ack_email", "email", "MOCK-email") in outbox(real)


def test_disabled_means_mock_with_a_note(mock_only: TestClient, backend: FakeBackend) -> None:
    c = mock_only
    v, token = use_invite(c)
    drain(c)
    assert backend.sent == []
    assert ("ack_email", "email", "MOCK-email") in outbox(c)
    assert invite_row(c, token).email_result == "mock:disabled"
    email = next(s for s in v.get(f"/try/{token}/status").json()["steps"] if s["key"] == "email")
    assert email["done"]
    assert email["real"] is False
    assert "turned off" in email["note"]


def test_sms_is_always_mock(real: TestClient) -> None:
    router = app_of(real).state.job_context.pick_sender
    assert isinstance(router, InterviewEmailRouter)
    assert router.real is not None
    use_invite(real)
    sms = MockSmsSender()
    with app_of(real).state.session_factory() as s:
        lead = s.scalar(select(Lead).where(Lead.source == INTERVIEW_SOURCE))
        assert lead is not None
        message = OutboundMessage(
            lead_id=lead.id,
            step="ack_sms",
            channel=Channel.SMS,
            to_address="+12025550123",
            body="hi",
            idempotency_key="k",
            transactional=True,
        )
        assert router(s, lead, message, sms, utcnow()) is sms
    # The job context only has mock senders; the router is the only way to a real one.
    senders = app_of(real).state.job_context.senders
    assert isinstance(senders[Channel.SMS], MockSmsSender)
    assert isinstance(senders[Channel.EMAIL], MockEmailSender)


def test_later_follow_ups_stay_mock(real: TestClient, backend: FakeBackend) -> None:
    c = real
    use_invite(c)
    drain(c)
    clock = FixedClock(utcnow() + timedelta(days=4))
    make_worker(app_of(c), clock).run_until_idle()
    assert len(backend.sent) == 1
    assert ("nudge_3d", "email", "MOCK-email") in outbox(c)


# --------------------------------------------------------------------------- caps


def test_hourly_cap() -> None:
    backend = FakeBackend()
    for c in _client(backend, **REAL, interview_email_max_per_hour=1):
        use_invite(c, "one@example.com", "first")
        drain(c)
        v, token = use_invite(c, "two@example.com", "second")
        drain(c)
        assert len(backend.sent) == 1
        assert invite_row(c, token).email_result == "mock:limit_hour"
        step = next(s for s in v.get(f"/try/{token}/status").json()["steps"] if s["key"] == "email")
        assert step["real"] is False
        assert "Demo limit reached" in step["note"]
        assert "1 used" in c.get("/dashboard/interview").text


def test_daily_cap_counts_older_sends(real: TestClient, backend: FakeBackend) -> None:
    c = real
    _, first = use_invite(c, "one@example.com", "first")
    drain(c)
    # Pretend 29 more real emails went out earlier today (outside the last hour).
    lead_id = lead_of(c, first).id
    with app_of(c).state.session_factory() as s:
        for i in range(29):
            s.add(
                OutboxMessage(
                    lead_id=lead_id,
                    step=f"old{i}",
                    channel="email",
                    to_address_enc="x",
                    subject="s",
                    body="b",
                    sender="SMTP-email",
                    idempotency_key=f"old:{i}",
                    sent_at=utcnow() - timedelta(hours=3),
                )
            )
        s.commit()
    _, token = use_invite(c, "two@example.com", "second")
    drain(c)
    assert len(backend.sent) == 1
    assert invite_row(c, token).email_result == "mock:limit_day"


def test_one_per_recipient_per_day(real: TestClient, backend: FakeBackend) -> None:
    c = real
    use_invite(c, VISITOR, "first")
    drain(c)
    v, token = use_invite(c, VISITOR.upper(), "second")  # same address, different case
    drain(c)
    assert len(backend.sent) == 1
    assert invite_row(c, token).email_result == "mock:limit_recipient"
    step = next(s for s in v.get(f"/try/{token}/status").json()["steps"] if s["key"] == "email")
    assert "this address today" in step["note"]


def test_one_per_invite(real: TestClient, backend: FakeBackend) -> None:
    c = real
    use_invite(c)
    drain(c)
    router = app_of(c).state.job_context.pick_sender
    mock = MockEmailSender()
    with app_of(c).state.session_factory() as s:
        lead = s.scalar(select(Lead).where(Lead.source == INTERVIEW_SOURCE))
        assert lead is not None
        again = OutboundMessage(
            lead_id=lead.id,
            step="ack_email_again",
            channel=Channel.EMAIL,
            to_address=VISITOR,
            body="hi",
            idempotency_key="again",
            transactional=True,
        )
        assert router(s, lead, again, mock, utcnow()) is mock
        invite = s.scalar(select(InterviewInvite))
        assert invite is not None
        assert invite.email_result == "sent"  # the note never hides that it was sent


def test_caps_router_unit() -> None:
    caps = EmailCaps(per_hour=10, per_day=30)
    assert InterviewEmailRouter(None, caps).caps.per_day == 30


# --------------------------------------------------------------------------- panel


def test_panel_steps(real: TestClient) -> None:
    c = real
    v, token = use_invite(c)
    page = v.get(f"/try/{token}").text
    assert 'data-status-url="/try/' in page
    assert '<script src="/static/try.js' in page
    assert 'href="/login"' in page
    assert v.get("/static/try.js").status_code == 200
    before = v.get(f"/try/{token}/status").json()
    keys = [s["key"] for s in before["steps"]]
    assert keys == ["created", "scored", "email", "crm"]
    assert before["steps"][0]["done"]
    assert not before["complete"]
    drain(c)
    after = v.get(f"/try/{token}/status").json()
    assert after["complete"]
    steps = {s["key"]: s for s in after["steps"]}
    assert steps["scored"]["score"] is not None
    assert steps["scored"]["summary"]
    assert steps["email"]["real"] is True
    assert steps["email"]["at"]
    assert steps["email"]["to"] == "v***@example.com"
    assert steps["crm"]["done"]
    assert after["sms"] == "Texts are never sent in interview mode."
    assert VISITOR not in str(after)


def test_panel_needs_the_visitors_cookie(real: TestClient) -> None:
    c = real
    _, token = use_invite(c)
    assert visitor(c).get(f"/try/{token}/status").status_code == 404
    other = visitor(c)
    other.cookies.set("stl_try", "guess", path=f"/try/{token}")
    assert other.get(f"/try/{token}/status").status_code == 404


def test_panel_without_crm(backend: FakeBackend) -> None:
    for c in _client(backend, **REAL, crm_provider="none"):
        v, token = use_invite(c)
        drain(c)
        data = v.get(f"/try/{token}/status").json()
        crm = next(s for s in data["steps"] if s["key"] == "crm")
        assert crm["done"]
        assert "turned off" in crm["detail"]
        assert data["complete"]


def test_panel_after_the_lead_is_deleted(real: TestClient) -> None:
    c = real
    v, token = use_invite(c)
    with app_of(c).state.session_factory() as s:
        s.execute(update(InterviewInvite).values(lead_id=None))
        s.commit()
    assert v.get(f"/try/{token}/status").json() == {"steps": [], "complete": True, "gone": True}


def test_delivery_failure_retries_and_never_stores_the_address(
    real: TestClient, backend: FakeBackend
) -> None:
    c = real
    backend.fail = True
    v, token = use_invite(c)
    drain(c)
    assert backend.sent == []
    assert invite_row(c, token).email_result is None  # rolled back with the job
    with app_of(c).state.session_factory() as s:
        job = s.scalar(select(Job).where(Job.payload["step"].as_string() == "ack_email"))
        assert job is not None
        assert job.status == JobStatus.QUEUED.value
        assert "SMTPRecipientsRefused" in (job.last_error or "")
        assert VISITOR not in (job.last_error or "")
        job.status = JobStatus.DEAD.value
        s.commit()
    email = next(s for s in v.get(f"/try/{token}/status").json()["steps"] if s["key"] == "email")
    assert email["failed"]
    assert "could not be delivered" in email["detail"]
    # Fixed backend + retry: it goes out once.
    backend.fail = False
    with app_of(c).state.session_factory() as s:
        s.execute(
            update(Job)
            .where(Job.payload["step"].as_string() == "ack_email")
            .values(status="queued", run_at=utcnow())
        )
        s.commit()
    drain(c)
    assert len(backend.sent) == 1


def test_smtp_rejection_through_the_pipeline_keeps_the_address_out(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The real SMTP backend against a local fake server that rejects the recipient."""
    from tests.unit.test_interview_email import FakeSMTPServer

    server = FakeSMTPServer(reject_rcpt=True)
    try:
        smtp = SmtpBackend(
            "127.0.0.1", server.port, username=None, password=None, starttls=False, timeout=5
        )
        for c in _client(smtp, **{**REAL, "smtp_host": "127.0.0.1"}, smtp_starttls=False):
            configure_logging("INFO", json_output=True)
            use_invite(c)
            drain(c)
            with app_of(c).state.session_factory() as s:
                errors = list(s.scalars(select(Job.last_error).where(Job.last_error.is_not(None))))
            assert errors
            assert all(VISITOR not in e for e in errors)
    finally:
        server.close()
    out = capsys.readouterr().out
    assert '"msg": "job_failed"' in out
    assert VISITOR not in out


# --------------------------------------------------------------------------- privacy


def test_recipient_is_masked_in_logs_and_encrypted_at_rest(
    capsys: pytest.CaptureFixture[str], real: TestClient
) -> None:
    c = real
    configure_logging("INFO", json_output=True)  # write to this test's captured stdout
    use_invite(c)
    drain(c)
    out = capsys.readouterr().out
    assert '"msg": "invite_used"' in out
    assert '"msg": "interview_email_sent"' in out
    assert "v***@example.com" in out
    assert VISITOR not in out
    with app_of(c).state.session_factory() as s:
        tables = [
            "leads",
            "outbox_messages",
            "interview_invites",
            "audit_events",
            "activities",
            "jobs",
        ]
        for table in tables:
            for row in s.execute(text(f"SELECT * FROM {table}")):  # noqa: S608
                assert VISITOR not in " ".join(str(v) for v in row), table
        msg = s.scalar(select(OutboxMessage).where(OutboxMessage.sender == "SMTP-email"))
        assert msg is not None
        assert msg.to_address == VISITOR  # decrypts with the key
        lead = s.scalar(select(Lead).where(Lead.source == INTERVIEW_SOURCE))
        assert lead is not None
        assert lead.email == VISITOR


# --------------------------------------------------------------------------- timing


def test_the_acknowledgment_is_not_held_by_the_sending_window(backend: FakeBackend) -> None:
    now_local = utcnow().astimezone(ZoneInfo("America/New_York"))
    closed = (now_local.hour + 12) % 24  # a one-hour window far from now
    window = f"mon-sun {closed:02d}:00-{closed:02d}:30"
    for c in _client(backend, **REAL, quiet_hours_enabled=True, send_windows=window):
        use_invite(c)
        assert c.post("/api/leads", json=valid_lead(email="api@example.com")).status_code == 201
        drain(c)
        assert len(backend.sent) == 1  # the invite's email went out right away
        sent = outbox(c)
        assert ("ack_email", "email", "SMTP-email") in sent
        assert ("ack_email", "email", "MOCK-email") not in sent  # the public lead waits


def test_the_kill_switch_holds_the_email(backend: FakeBackend) -> None:
    for c in _client(backend, **REAL, sends_paused=True):
        v, token = use_invite(c)
        drain(c)
        assert backend.sent == []
        email = next(
            s for s in v.get(f"/try/{token}/status").json()["steps"] if s["key"] == "email"
        )
        assert not email["done"]
        assert "failed" not in email


def test_link_uses_https_behind_a_trusted_proxy() -> None:
    for c in _client(trust_forwarded_for=True):
        login_as(c, UserRole.ADMIN)
        r = c.post(
            "/dashboard/interview",
            data={"label": "x", "csrf_token": csrf(c, "/dashboard/interview")},
            headers={"x-forwarded-proto": "https"},
        )
        assert r.status_code == 201
        assert "https://localhost/try/" in r.text


def test_the_real_sender_refuses_sms(real: TestClient) -> None:
    router = app_of(real).state.job_context.pick_sender
    message = OutboundMessage(
        lead_id=valid_uuid(),
        step="ack_sms",
        channel=Channel.SMS,
        to_address="+12025550123",
        body="hi",
        idempotency_key="k",
        transactional=True,
    )
    with app_of(real).state.session_factory() as s, pytest.raises(ValueError, match="only"):
        router.real.send(s, message, now=utcnow())


def valid_uuid() -> Any:
    import uuid

    return uuid.uuid4()
