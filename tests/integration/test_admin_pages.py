"""Admin pages: users, kill switch and settings, dead-letter jobs, audit log; task closing."""

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import DEFAULT_DEMO_PASSWORD
from app.models import AuditEvent, Job, JobStatus, SystemFlag, Task, User, UserRole, utcnow
from tests.conftest import ADMIN, PASSWORD, ROLE_EMAILS, add_user, drain, seed_lead
from tests.web_helpers import app_of, login, login_as, make_client, post


@pytest.fixture
def web() -> Iterator[TestClient]:
    for c in make_client():
        login_as(c, UserRole.ADMIN)
        yield c


def _user(c: TestClient, email: str) -> User:
    with app_of(c).state.session_factory() as s:
        user = s.scalar(select(User).where(User.email == email))
        assert user is not None
        return user


def _follow(c: TestClient, r: object) -> str:
    location = r.headers["location"]  # type: ignore[attr-defined]
    return c.get(location).text


# --------------------------------------------------------------------------- users


def test_create_user_and_validation_messages(web: TestClient) -> None:
    url = "/dashboard/admin/users"
    r = post(web, url, {"email": "Dana@Example.com", "role": "rep", "password": PASSWORD})
    assert "User created." in _follow(web, r)
    assert _user(web, "dana@example.com").role == "rep"  # stored lowercased
    cases = {
        ("dana@example.com", "rep", PASSWORD): "already exists",
        ("not-an-email", "rep", PASSWORD): "valid email",
        ("lee@example.com", "owner", PASSWORD): "admin, rep or viewer",
        ("lee@example.com", "rep", "short"): "12 to 128 characters",
        ("lee@example.com", "rep", "lee@example.com"): "12 to 128 characters",
    }
    for (email, role, password), message in cases.items():
        r = post(web, url, {"email": email, "role": role, "password": password})
        assert message in _follow(web, r)
    page = web.get(url).text
    assert "dana@example.com" in page
    assert PASSWORD not in page
    assert "argon2" not in page  # hashes never reach the page


def test_update_role_and_active_ends_their_sessions(web: TestClient) -> None:
    rep_id = add_user(app_of(web), UserRole.REP)
    other = next(make_client())
    other_app = app_of(other)  # a separate app; use the same one instead:
    del other_app
    rep_client = TestClient(web.app, base_url="http://localhost", follow_redirects=False)
    assert login(rep_client, email=ROLE_EMAILS[UserRole.REP]).status_code == 303
    assert rep_client.get("/dashboard").status_code == 200
    r = post(web, f"/dashboard/admin/users/{rep_id}", {"role": "viewer", "active": "1"})
    assert "sessions were ended" in _follow(web, r)
    assert rep_client.get("/dashboard").status_code == 303  # logged out by the change
    assert _user(web, ROLE_EMAILS[UserRole.REP]).role == "viewer"
    r = post(web, f"/dashboard/admin/users/{rep_id}", {"role": "viewer"})  # unchecked: inactive
    assert not _user(web, ROLE_EMAILS[UserRole.REP]).active
    with app_of(web).state.session_factory() as s:
        changes = [
            e.summary["changes"]
            for e in s.scalars(select(AuditEvent).where(AuditEvent.action == "user_updated"))
        ]
    assert changes == [{"role": ["rep", "viewer"]}, {"active": [True, False]}]
    # Saving without a change writes no audit event.
    post(web, f"/dashboard/admin/users/{rep_id}", {"role": "viewer"})
    r = post(web, f"/dashboard/admin/users/{rep_id}", {"role": "boss", "active": "1"})
    assert "admin, rep or viewer" in _follow(web, r)
    r = post(web, "/dashboard/admin/users/99999", {"role": "rep", "active": "1"})
    assert "does not exist" in _follow(web, r)


def test_admins_cannot_lock_themselves_out(web: TestClient) -> None:
    me = _user(web, ROLE_EMAILS[UserRole.ADMIN])
    r = post(web, f"/dashboard/admin/users/{me.id}", {"role": "rep", "active": "1"})
    assert "your own role" in _follow(web, r)
    r = post(web, f"/dashboard/admin/users/{me.id}", {"role": "admin"})  # deactivate self
    assert "your own role" in _follow(web, r)
    other_admin = add_user(app_of(web), UserRole.ADMIN, email="second@example.com")
    with app_of(web).state.session_factory() as s:
        u = s.get(User, me.id)
        assert u is not None
        u.active = False  # pretend only the other admin is active
        s.commit()
    from app.services.users import UserError, update_user

    with app_of(web).state.session_factory() as s:
        target = s.get(User, other_admin)
        assert target is not None
        with pytest.raises(UserError, match="at least one active admin"):
            update_user(s, target, acting_user_id=None, role="rep", now=utcnow())


def test_unlock_and_reset_password(web: TestClient) -> None:
    rep_id = add_user(app_of(web), UserRole.REP)
    with app_of(web).state.session_factory() as s:
        u = s.get(User, rep_id)
        assert u is not None
        u.failed_logins, u.locked_until = 7, utcnow().replace(year=2099)
        s.commit()
    page = web.get("/dashboard/admin/users").text
    assert "locked" in page
    assert "Account unlocked." in _follow(web, post(web, f"/dashboard/admin/users/{rep_id}/unlock"))
    assert _user(web, ROLE_EMAILS[UserRole.REP]).locked_until is None
    r = post(web, f"/dashboard/admin/users/{rep_id}/password", {"password": "short"})
    assert "12 to 128" in _follow(web, r)
    r = post(web, f"/dashboard/admin/users/{rep_id}/password", {"password": "a new long password"})
    assert "Password reset." in _follow(web, r)
    rep = TestClient(web.app, base_url="http://localhost", follow_redirects=False)
    assert login(rep, email=ROLE_EMAILS[UserRole.REP]).status_code == 401
    assert (
        login(rep, email=ROLE_EMAILS[UserRole.REP], password="a new long password").status_code
        == 303
    )
    for path in ("unlock", "password"):
        r = post(web, f"/dashboard/admin/users/99999/{path}", {"password": "a new long password"})
        assert "does not exist" in _follow(web, r)


def test_demo_mode_turns_user_changes_off(tmp_path: object) -> None:
    for c in make_client(demo_mode=True, demo_leads=0, database_url="sqlite://"):
        from app.security.totp import demo_secret
        from tests.web_helpers import pass_2fa

        r = pass_2fa(c, login(c, password=DEFAULT_DEMO_PASSWORD), demo_secret())
        assert r.status_code == 303
        page = c.get("/dashboard/admin/users").text
        assert "user changes are turned off" in page
        rep = _user(c, ROLE_EMAILS[UserRole.REP])
        for url, data in (
            (
                "/dashboard/admin/users",
                {"email": "x@example.com", "role": "rep", "password": PASSWORD},
            ),
            (f"/dashboard/admin/users/{rep.id}", {"role": "admin", "active": "1"}),
            (f"/dashboard/admin/users/{rep.id}/password", {"password": "another long one"}),
        ):
            assert "turned off in the public demo" in _follow(c, post(c, url, data))
        assert _user(c, ROLE_EMAILS[UserRole.REP]).role == "rep"


# --------------------------------------------------------------------------- system


def test_kill_switch_page_pauses_and_resumes(web: TestClient) -> None:
    page = web.get("/dashboard/admin/system").text
    assert "running" in page
    assert "Pause all sends" in page
    for label in ("AI provider", "CRM adapter", "Retention", "CORS origins"):
        assert label in page
    assert "unit-test" not in page  # no secret values
    r = post(web, "/dashboard/admin/kill-switch", {"paused": "1"})
    assert "Saved." in _follow(web, r)
    with app_of(web).state.session_factory() as s:
        flag = s.get(SystemFlag, "sends_paused")
        assert flag is not None
        assert flag.value == "true"
        assert flag.updated_by == "human:admin"
    assert "Resume sends" in web.get("/dashboard/admin/system").text
    r = post(web, "/dashboard/admin/kill-switch", {"paused": "0"})
    assert r.status_code == 303
    assert "Pause all sends" in web.get("/dashboard/admin/system").text


def test_env_kill_switch_is_shown() -> None:
    for c in make_client(sends_paused=True):
        login_as(c, UserRole.ADMIN)
        assert "Paused by the environment" in c.get("/dashboard/admin/system").text


# --------------------------------------------------------------------------- jobs


def test_dead_letter_page_and_retry(web: TestClient) -> None:
    assert "No dead jobs" in web.get("/dashboard/admin/jobs").text
    seed_lead(app_of(web))
    with app_of(web).state.session_factory() as s:
        job = s.scalars(select(Job)).first()
        assert job is not None
        job.status, job.last_error = JobStatus.DEAD.value, "boom: simulated"
        s.commit()
        job_id = job.id
    page = web.get("/dashboard/admin/jobs").text
    assert "boom: simulated" in page
    r = post(web, f"/dashboard/admin/jobs/{job_id}/retry")
    assert f"Job {job_id} is queued again" in _follow(web, r)
    r = post(web, f"/dashboard/admin/jobs/{job_id}/retry")  # not dead any more
    assert "could not be retried" in _follow(web, r)
    r = post(web, "/dashboard/admin/jobs/999999/retry")
    assert "could not be retried" in _follow(web, r)
    with app_of(web).state.session_factory() as s:
        outcomes = [
            e.outcome
            for e in s.scalars(select(AuditEvent).where(AuditEvent.action == "job_retried"))
        ]
    assert outcomes == ["ok", "failed", "failed"]


# --------------------------------------------------------------------------- audit log


def test_audit_log_page_filters_and_pages(web: TestClient) -> None:
    from app.services import audit

    with app_of(web).state.session_factory() as s:
        for i in range(60):
            audit.record(
                s,
                actor="cli",
                role=None,
                action="user_created" if i % 2 else "kill_switch",
                target_type="user",
                target_id=i,
                summary={"note": f"call (202) 555-01{i % 100:02d} or x{i}@example.com"},
            )
        s.commit()
    page = web.get("/dashboard/admin/audit").text
    assert "Page 1 of 2" in page
    assert "555-01" not in page  # summaries are masked
    assert "x59@example.com" not in page
    assert "x***@example.com" in page
    assert 'rel="next"' in page
    assert 'rel="prev"' in web.get("/dashboard/admin/audit?page=2").text
    filtered = web.get("/dashboard/admin/audit?action=kill_switch&actor=cli&outcome=ok").text
    assert "30 matching" in filtered
    assert "1 matching" in web.get("/dashboard/admin/audit?target=7").text
    assert "0 matching" in web.get("/dashboard/admin/audit?outcome=denied").text
    # Unknown filter values are ignored rather than echoed.
    assert (
        "61 matching" in web.get("/dashboard/admin/audit?action=nope&outcome=maybe").text
    )  # + the login


def test_machine_api_actions_are_audited(web: TestClient) -> None:
    lead_id = seed_lead(app_of(web))
    drain(web)
    r = web.patch(
        f"/api/leads/{lead_id}/stage", json={"stage": "qualified", "actor": "dana"}, headers=ADMIN
    )
    assert r.status_code == 200
    assert (
        web.post(
            "/admin/kill-switch", json={"paused": True, "actor": "dana"}, headers=ADMIN
        ).status_code
        == 200
    )
    with app_of(web).state.session_factory() as s:
        task = Task(lead_id=lead_id, reason="lead_replied")
        s.add(task)
        job = s.scalars(select(Job)).first()
        assert job is not None
        job.status = JobStatus.DEAD.value
        s.commit()
        task_id, job_id = task.id, job.id
    assert (
        web.patch(f"/api/tasks/{task_id}", json={"assignee": "lee"}, headers=ADMIN).status_code
        == 200
    )
    assert (
        web.patch(f"/api/tasks/{task_id}", json={"status": "done"}, headers=ADMIN).status_code
        == 200
    )
    assert web.post(f"/admin/jobs/{job_id}/retry", headers=ADMIN).status_code == 200
    with app_of(web).state.session_factory() as s:
        rows = {
            (e.action, e.actor)
            for e in s.scalars(select(AuditEvent).where(AuditEvent.actor == "api-token"))
        }
    assert rows == {
        ("stage_changed", "api-token"),
        ("kill_switch", "api-token"),
        ("task_updated", "api-token"),
        ("task_closed", "api-token"),
        ("job_retried", "api-token"),
    }


# --------------------------------------------------------------------------- tasks


def _handoff_lead(c: TestClient) -> tuple[uuid.UUID, int]:
    from app.services.handoff import LEAD_REPLIED, handoff

    lead_id = seed_lead(app_of(c))
    with app_of(c).state.session_factory() as s:
        from app.models import Lead

        lead = s.get(Lead, lead_id)
        assert lead is not None
        task = handoff(s, lead, LEAD_REPLIED, now=utcnow(), detail="call me")
        s.commit()
        assert task is not None
        return lead_id, task.id


def test_close_task_with_resume_and_its_errors(web: TestClient) -> None:
    lead_id, task_id = _handoff_lead(web)
    with app_of(web).state.session_factory() as s:
        extra = Task(lead_id=lead_id, reason="ai_human_review")
        s.add(extra)
        s.commit()
        extra_id = extra.id
    url = f"/dashboard/tasks/{task_id}/close"
    r = post(web, url, {"resume_sequence": "1"})
    assert r.headers["location"].endswith("error=other_open_tasks")
    assert "other open tasks" in _follow(web, r)
    assert post(web, f"/dashboard/tasks/{extra_id}/close").status_code == 303
    r = post(web, url, {"resume_sequence": "1"})
    assert "Task closed." in _follow(web, r)
    r = post(web, url)
    assert "already closed" in _follow(web, r)
    assert post(web, "/dashboard/tasks/999999/close").status_code == 404
    page = web.get(f"/dashboard/leads/{lead_id}").text
    assert "Follow-ups resumed" in page
    assert "Task closed (lead_replied)" in page


def test_close_task_resume_refused_after_opt_out(web: TestClient) -> None:
    lead_id, task_id = _handoff_lead(web)
    with app_of(web).state.session_factory() as s:
        from app.models import Lead

        lead = s.get(Lead, lead_id)
        assert lead is not None
        lead.opted_out_at = utcnow()
        s.commit()
    r = post(web, f"/dashboard/tasks/{task_id}/close", {"resume_sequence": "1"})
    assert r.headers["location"].endswith("error=opted_out")
    with app_of(web).state.session_factory() as s:
        assert s.scalar(select(Task.status).where(Task.id == task_id)) == "open"


def test_lead_list_assignment_filter(web: TestClient) -> None:
    me = _user(web, ROLE_EMAILS[UserRole.ADMIN]).id
    a = seed_lead(app_of(web))
    seed_lead(app_of(web), phone="(202) 555-0170", email="b@example.com")
    with app_of(web).state.session_factory() as s:
        from app.models import Lead

        lead = s.get(Lead, a)
        assert lead is not None
        lead.assignee_id = me
        s.commit()
    assert "1 matching" in web.get("/dashboard/leads?assigned=me").text
    assert "1 matching" in web.get("/dashboard/leads?assigned=none").text
    page = web.get("/dashboard/leads?assigned=me&page=1").text
    assert ">admin<" in page
    assert web.get("/dashboard/leads?assigned=bogus").status_code == 422
