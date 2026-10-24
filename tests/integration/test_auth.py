"""Login, sessions, lockout, logout (email + password accounts)."""

from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import AuditEvent, User, UserRole, UserSession, utcnow
from app.security import passwords
from app.security.sessions import SECURE_SESSION_COOKIE, SESSION_COOKIE
from tests.conftest import PASSWORD, ROLE_EMAILS, add_user
from tests.web_helpers import app_of, csrf, login, login_csrf, make_client, set_cookie

ADMIN_EMAIL = ROLE_EMAILS[UserRole.ADMIN]


@pytest.fixture
def web() -> Iterator[TestClient]:
    for c in make_client():
        add_user(app_of(c), UserRole.ADMIN)
        yield c


def _events(c: TestClient, action: str) -> list[AuditEvent]:
    with app_of(c).state.session_factory() as s:
        return list(s.scalars(select(AuditEvent).where(AuditEvent.action == action)))


def _user(c: TestClient, email: str = ADMIN_EMAIL) -> User:
    with app_of(c).state.session_factory() as s:
        user = s.scalar(select(User).where(User.email == email))
        assert user is not None
        return user


def _age_sessions(c: TestClient, **delta: float) -> None:
    with app_of(c).state.session_factory() as s:
        for row in s.scalars(select(UserSession)):
            row.created_at -= timedelta(**delta)
            row.last_seen_at -= timedelta(**delta)
            row.expires_at -= timedelta(**delta)
        s.commit()


def test_pages_redirect_to_login_and_login_page_is_plain(web: TestClient) -> None:
    r = web.get("/dashboard/leads?stage=new")
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=%2Fdashboard%2Fleads%3Fstage%3Dnew"
    page = web.get("/login")
    assert page.status_code == 200
    assert 'name="email"' in page.text
    assert 'name="password"' in page.text
    assert "Demo accounts" not in page.text  # only in demo mode
    assert "DEMO" in page.text  # the banner is always there


def test_login_sets_a_hardened_session_cookie(web: TestClient) -> None:
    r = login(web, next_path="/dashboard/leads")
    assert r.status_code == 303
    assert r.headers["location"] == "/dashboard/leads"
    morsel = set_cookie(r, SESSION_COOKIE)[SESSION_COOKIE]
    assert morsel["httponly"]
    assert morsel["samesite"] == "strict"
    assert not morsel["secure"]  # plain-HTTP localhost development
    assert morsel["path"] == "/"
    assert int(morsel["max-age"]) == 12 * 3600
    assert PASSWORD not in morsel.value
    with app_of(web).state.session_factory() as s:
        row = s.scalars(select(UserSession)).one()
        assert morsel.value.split(".")[0] not in row.token_hash  # only a hash is stored
    assert web.get("/dashboard").status_code == 200
    assert web.get("/login").status_code == 303  # already logged in
    [event] = _events(web, "login")
    assert (event.actor, event.outcome, event.role) == (ADMIN_EMAIL, "ok", "admin")
    assert event.ip_hash
    assert "testclient" not in event.ip_hash


def test_cookie_is_secure_with_host_prefix_off_localhost() -> None:
    for c in make_client(base_url="https://dash.example.com"):
        add_user(app_of(c), UserRole.ADMIN)
        assert set_cookie(c.get("/login"), "stl_csrf")["stl_csrf"]["secure"]
        r = login(c)
        assert r.status_code == 303
        morsel = set_cookie(r, SECURE_SESSION_COOKIE)[SECURE_SESSION_COOKIE]
        assert morsel["secure"]
        assert morsel["path"] == "/"
        assert not morsel["domain"]
        assert c.get("/dashboard").status_code == 200


@pytest.mark.parametrize(
    ("email", "password"),
    [
        (ADMIN_EMAIL, "wrong password here"),
        ("nobody@example.com", PASSWORD),
        ("", ""),
        ("x" * 300 + "@example.com", PASSWORD),
        (ADMIN_EMAIL, "p" * 5000),
    ],
)
def test_login_failures_are_generic(web: TestClient, email: str, password: str) -> None:
    r = login(web, email=email, password=password)
    assert r.status_code == 401
    assert "did not work" in r.text
    assert SESSION_COOKIE not in r.headers.get("set-cookie", "")
    assert web.get("/dashboard").status_code == 303
    [event] = _events(web, "login")
    assert event.outcome == "failed"


def test_login_needs_csrf(web: TestClient) -> None:
    r = web.post("/login", data={"email": ADMIN_EMAIL, "password": PASSWORD, "csrf_token": "x"})
    assert r.status_code == 403
    login_csrf(web)
    r = web.post(
        "/login",
        data={"email": ADMIN_EMAIL, "password": PASSWORD, "csrf_token": web.cookies["stl_csrf"]},
        headers={"Origin": "https://evil.example.net"},
    )
    assert r.status_code == 403


def test_unknown_email_still_spends_a_hash_verification(
    web: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    real = passwords.dummy_verify
    monkeypatch.setattr(passwords, "dummy_verify", lambda: calls.append("dummy") or real())
    login(web, email="nobody@example.com")
    assert calls == ["dummy"]


def test_lockout_after_repeated_failures_with_backoff(web: TestClient) -> None:
    for _ in range(4):
        assert login(web, password="wrong password here").status_code == 401
    assert _user(web).locked_until is None
    assert login(web, password="wrong password here").status_code == 401  # 5th: locks
    user = _user(web)
    assert user.failed_logins == 5
    assert user.locked_until is not None
    first_lock = user.locked_until - utcnow()
    assert timedelta(seconds=50) < first_lock <= timedelta(seconds=60)
    # Locked: the right password is refused with the same message, and not counted.
    r = login(web)
    assert r.status_code == 401
    assert "did not work" in r.text
    assert _user(web).failed_logins == 5
    # After the lock ends, the next failure locks for twice as long.
    with app_of(web).state.session_factory() as s:
        locked = s.scalar(select(User).where(User.email == ADMIN_EMAIL))
        assert locked is not None
        locked.locked_until = utcnow() - timedelta(seconds=1)
        s.commit()
    login(web, password="wrong password here")
    second_lock = _user(web).locked_until - utcnow()  # type: ignore[operator]
    assert timedelta(seconds=110) < second_lock <= timedelta(seconds=120)
    assert any(e.summary.get("locked") for e in _events(web, "login"))


def test_success_resets_the_failure_count(web: TestClient) -> None:
    login(web, password="wrong password here")
    assert _user(web).failed_logins == 1
    assert login(web).status_code == 303
    user = _user(web)
    assert user.failed_logins == 0
    assert user.last_login_at is not None


def test_login_is_rate_limited_per_ip() -> None:
    for c in make_client(login_rate_limit_per_minute=3):
        statuses = [login(c, email="nobody@example.com").status_code for _ in range(5)]
        assert statuses[:3] == [401, 401, 401]
        assert statuses[3] == 429


def test_inactive_user_cannot_log_in(web: TestClient) -> None:
    add_user(app_of(web), UserRole.REP, active=False)
    assert login(web, email=ROLE_EMAILS[UserRole.REP]).status_code == 401


@pytest.mark.parametrize(
    "target", ["//evil.example.com", "https://evil.example.com", "/docs", "/dashboard\\x"]
)
def test_login_never_redirects_off_the_dashboard(web: TestClient, target: str) -> None:
    assert login(web, next_path=target).headers["location"] == "/dashboard"


def test_session_rotates_on_login(web: TestClient) -> None:
    login(web)
    first = web.cookies.get(SESSION_COOKIE)
    login(web)
    second = web.cookies.get(SESSION_COOKIE)
    assert first
    assert second
    assert first != second
    web.cookies.set(SESSION_COOKIE, first)
    assert web.get("/dashboard").status_code == 303  # the pre-rotation session is dead
    with app_of(web).state.session_factory() as s:
        rows = list(s.scalars(select(UserSession).order_by(UserSession.id)))
    assert rows[0].revoked_at is not None
    assert rows[1].revoked_at is None


def test_tampered_or_garbage_session_is_rejected(web: TestClient) -> None:
    login(web)
    cookie = web.cookies.get(SESSION_COOKIE)
    assert cookie
    sid, mac = cookie.split(".")
    for bad in (f"{sid}x.{mac}", f"{sid}.{mac[::-1]}", "garbage", "a.b.c", "x" * 300):
        web.cookies.set(SESSION_COOKIE, bad)
        assert web.get("/dashboard").status_code == 303
    web.cookies.set(SESSION_COOKIE, cookie)
    assert web.get("/dashboard").status_code == 200


def test_logout_revokes_the_session_on_the_server(web: TestClient) -> None:
    login(web)
    cookie = web.cookies.get(SESSION_COOKIE)
    assert web.post("/logout", data={"csrf_token": "x"}).status_code == 403
    r = web.post("/logout", data={"csrf_token": csrf(web)})
    assert r.status_code == 303
    assert "Max-Age=0" in r.headers["set-cookie"] or 'stl_session=""' in r.headers["set-cookie"]
    web.cookies.set(SESSION_COOKIE, cookie or "")  # replaying the old cookie does not work
    assert web.get("/dashboard").status_code == 303
    assert [e.outcome for e in _events(web, "logout")] == ["ok"]
    # Logging out without a session just goes to the login page.
    web.cookies.clear()
    assert web.post("/logout").headers["location"] == "/login"


def test_idle_timeout(web: TestClient) -> None:
    login(web)
    _age_sessions(web, minutes=31)
    assert web.get("/dashboard").status_code == 303


def test_activity_extends_idle_but_background_refresh_does_not(web: TestClient) -> None:
    login(web)
    _age_sessions(web, minutes=20)
    # Polling does not count as activity...
    r = web.get("/api/metrics/kpis", headers={"X-Background-Refresh": "1"})
    assert r.status_code == 200
    with app_of(web).state.session_factory() as s:
        row = s.scalars(select(UserSession)).one()
        assert utcnow() - row.last_seen_at > timedelta(minutes=19)
    # ...a page view does.
    assert web.get("/dashboard").status_code == 200
    with app_of(web).state.session_factory() as s:
        row = s.scalars(select(UserSession)).one()
        assert utcnow() - row.last_seen_at < timedelta(minutes=1)


def test_absolute_timeout(web: TestClient) -> None:
    login(web)
    with app_of(web).state.session_factory() as s:
        row = s.scalars(select(UserSession)).one()
        row.expires_at = utcnow() - timedelta(seconds=1)  # 12h reached, even if active
        s.commit()
    assert web.get("/dashboard").status_code == 303


def test_deactivated_user_loses_live_sessions(web: TestClient) -> None:
    login(web)
    with app_of(web).state.session_factory() as s:
        user = s.scalar(select(User).where(User.email == ADMIN_EMAIL))
        assert user is not None
        user.active = False
        s.commit()
    assert web.get("/dashboard").status_code == 303


def test_password_hash_is_upgraded_on_login(web: TestClient, monkeypatch: Any) -> None:
    from argon2 import PasswordHasher

    old_hash = _user(web).password_hash
    passwords.configure(PasswordHasher(time_cost=2, memory_cost=1024, parallelism=1))
    try:
        assert login(web).status_code == 303
    finally:
        passwords.configure(PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1))
    assert _user(web).password_hash != old_hash
