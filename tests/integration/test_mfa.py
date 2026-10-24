"""Two-factor (TOTP): enrollment, the login step, recovery codes, lockout, the admin policy."""

import re
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from app.models import AuditEvent, RecoveryCode, User, UserRole, UserSession, utcnow
from app.security import totp
from app.security.sessions import MFA_COOKIE, SESSION_COOKIE
from app.services import mfa
from tests.conftest import PASSWORD, ROLE_EMAILS, add_user
from tests.web_helpers import app_of, csrf, login, make_client, post, set_cookie

ACCOUNT = "/dashboard/account"
SECRET = totp.demo_secret()  # any valid base32 secret will do for tests


def code(secret: str = SECRET, steps: int = 0) -> str:
    """The code ``steps`` periods from now (drift of one step is accepted)."""
    return totp.current_code(secret, utcnow() + timedelta(seconds=totp.PERIOD * steps))


def _enable(c: TestClient, role: UserRole) -> tuple[int, list[str]]:
    app = app_of(c)
    user_id = add_user(app, role)
    with app.state.session_factory() as s:
        user = s.get(User, user_id)
        assert user is not None
        codes = mfa.enable_with_secret(s, user, SECRET, now=utcnow())
        s.commit()
    return user_id, codes


def _second_step(c: TestClient, value: str, next_path: str = "/dashboard") -> Any:
    return c.post(
        "/login/2fa", data={"code": value, "csrf_token": csrf(c, "/login"), "next": next_path}
    )


def _user(c: TestClient, user_id: int) -> User:
    with app_of(c).state.session_factory() as s:
        user = s.get(User, user_id)
        assert user is not None
        return user


def _actions(c: TestClient) -> list[tuple[str, str]]:
    with app_of(c).state.session_factory() as s:
        return [
            (e.action, e.outcome) for e in s.scalars(select(AuditEvent).order_by(AuditEvent.id))
        ]


@pytest.fixture
def web() -> Iterator[TestClient]:
    yield from make_client()


@pytest.fixture
def strict() -> Iterator[TestClient]:
    yield from make_client(admin_2fa_required=True)


# --------------------------------------------------------------------------- login step


def test_password_alone_does_not_log_in_when_2fa_is_on(web: TestClient) -> None:
    _enable(web, UserRole.REP)
    r = login(web, email=ROLE_EMAILS[UserRole.REP])
    assert r.status_code == 303
    assert r.headers["location"] == "/login/2fa?next=%2Fdashboard"
    pending = set_cookie(r, MFA_COOKIE)[MFA_COOKIE]
    assert pending["httponly"]
    assert pending["samesite"] == "strict"
    assert pending["path"] == "/login"
    assert SESSION_COOKIE not in r.headers.get("set-cookie", "")
    assert web.get("/dashboard").status_code == 303  # still logged out
    page = web.get("/login/2fa")
    assert page.status_code == 200
    assert 'autocomplete="one-time-code"' in page.text
    assert "Demo:" not in page.text  # the demo hint only exists in demo mode


def test_right_code_logs_in_and_a_code_cannot_be_replayed(web: TestClient) -> None:
    user_id, _ = _enable(web, UserRole.REP)
    login(web, email=ROLE_EMAILS[UserRole.REP])
    assert _second_step(web, "000000").status_code == 401
    good = code()
    r = _second_step(web, good)
    assert r.status_code == 303
    assert r.headers["location"] == "/dashboard"
    assert web.get("/dashboard").status_code == 200
    assert "stl_mfa=" in r.headers.get("set-cookie", "")  # the pending cookie is cleared
    # Log out and in again with the same code: refused (replay), the next one works.
    web.post("/logout", data={"csrf_token": csrf(web)})
    login(web, email=ROLE_EMAILS[UserRole.REP])
    assert _second_step(web, good).status_code == 401
    assert _second_step(web, code(steps=1)).status_code == 303
    actions = _actions(web)
    assert ("mfa_verify", "failed") in actions
    assert ("mfa_verify", "ok") in actions
    assert _user(web, user_id).failed_logins == 0


def test_recovery_code_works_once(web: TestClient) -> None:
    _user_id, codes = _enable(web, UserRole.REP)
    assert len(codes) == 10
    assert len(set(codes)) == 10
    login(web, email=ROLE_EMAILS[UserRole.REP])
    assert _second_step(web, codes[0].upper().replace("-", " ")).status_code == 303
    with app_of(web).state.session_factory() as s:
        event = s.scalars(select(AuditEvent).where(AuditEvent.action == "mfa_recovery_used")).one()
        assert event.summary == {"method": "recovery", "codes_left": 9}
        assert (
            s.scalar(select(func.count(RecoveryCode.id)).where(RecoveryCode.used_at.is_not(None)))
            == 1
        )
    web.cookies.clear()
    login(web, email=ROLE_EMAILS[UserRole.REP])
    assert _second_step(web, codes[0]).status_code == 401  # used up
    assert _second_step(web, "zzzzz-zzzzz").status_code == 401  # not a code at all


def test_bad_codes_lock_the_account_and_the_password_does_not_reset_it(web: TestClient) -> None:
    user_id, _ = _enable(web, UserRole.REP)
    for _ in range(3):
        login(web, email=ROLE_EMAILS[UserRole.REP])  # right password each time...
        assert _second_step(web, "123456").status_code == 401
    assert _user(web, user_id).failed_logins == 3  # ...but the count keeps going
    login(web, email=ROLE_EMAILS[UserRole.REP])
    _second_step(web, "123456")
    _second_step(web, "123456")
    user = _user(web, user_id)
    assert user.locked_until is not None
    r = _second_step(web, code())  # locked: even the right code fails, same message
    assert r.status_code == 401
    assert "did not work" in r.text
    with app_of(web).state.session_factory() as s:
        assert any(e.summary.get("locked") for e in s.scalars(select(AuditEvent)))


def test_second_step_needs_a_genuine_fresh_pending_cookie(web: TestClient) -> None:
    user_id, _ = _enable(web, UserRole.REP)
    assert web.get("/login/2fa").headers["location"] == "/login"
    assert web.post("/login/2fa", data={"code": code()}).headers["location"] == "/login"
    login(web, email=ROLE_EMAILS[UserRole.REP])
    pending = web.cookies.get(MFA_COOKIE)
    assert pending
    uid, issued, tag, mac = pending.split(".")
    for bad in (
        f"{uid}.{issued}.{tag}.{mac[::-1]}",
        f"{int(uid) + 1}.{issued}.{tag}.{mac}",
        "garbage",
        "x.y.z.w",
    ):
        web.cookies.set(MFA_COOKIE, bad, path="/login")
        assert web.get("/login/2fa").status_code == 303
    web.cookies.clear()
    login(web, email=ROLE_EMAILS[UserRole.REP])
    assert web.post("/login/2fa", data={"code": code(), "csrf_token": "x"}).status_code == 403
    # A password change in the meantime kills the pending step.
    with app_of(web).state.session_factory() as s:
        user = s.get(User, user_id)
        assert user is not None
        user.password_hash = "changed"  # pragma: allowlist secret
        s.commit()
    assert web.get("/login/2fa").status_code == 303


def test_pending_step_expires(web: TestClient) -> None:
    from app.security.sessions import read_pending

    _user_id, _ = _enable(web, UserRole.REP)
    login(web, email=ROLE_EMAILS[UserRole.REP])
    pending = web.cookies.get(MFA_COOKIE)
    key = app_of(web).state.settings.secret_key_bytes
    with app_of(web).state.session_factory() as s:
        assert read_pending(s, key, pending, now=utcnow()) is not None
        assert read_pending(s, key, pending, now=utcnow() + timedelta(minutes=6)) is None


# --------------------------------------------------------------------------- enrollment


def test_rep_enrolls_with_a_server_side_qr_code(web: TestClient) -> None:
    rep = add_user(app_of(web), UserRole.REP)
    login(web, email=ROLE_EMAILS[UserRole.REP])
    page = web.get("/dashboard/account").text
    assert "Optional for your role" in page
    r = post(web, "/dashboard/account/2fa/start")
    assert r.status_code == 200
    qr = re.search(r'src="(data:image/svg\+xml[^"]+)"', r.text)
    assert qr  # drawn here, not by an outside QR service
    assert "http" not in qr.group(1).split(",", 1)[0]
    secret = re.search(r'<code class="pick">([A-Z2-7]{32})</code>', r.text)
    assert secret
    assert post(web, "/dashboard/account/2fa/confirm", {"code": "000000"}).status_code == 422
    done = post(web, "/dashboard/account/2fa/confirm", {"code": code(secret.group(1))})
    assert done.status_code == 200
    shown = re.findall(r'<code class="pick">([a-z2-9]{5}-[a-z2-9]{5})</code>', done.text)
    assert len(shown) == 10
    with app_of(web).state.session_factory() as s:
        raw = " ".join(str(v) for row in s.execute(text("SELECT * FROM users")).all() for v in row)
        assert secret.group(1) not in raw  # encrypted at rest
        hashes = list(s.scalars(select(RecoveryCode.code_hash).where(RecoveryCode.user_id == rep)))
        assert len(hashes) == 10
        assert not set(shown) & set(hashes)  # only hashes stored
    assert ("mfa_enrolled", "ok") in _actions(web)
    assert ("mfa_enrolled", "failed") in _actions(web)
    assert "Status: <strong>on</strong>" in web.get("/dashboard/account").text


def test_confirm_without_starting(web: TestClient) -> None:
    add_user(app_of(web), UserRole.REP)
    login(web, email=ROLE_EMAILS[UserRole.REP])
    r = post(web, "/dashboard/account/2fa/confirm", {"code": "123456"})
    assert r.status_code == 422
    assert "Start the set-up again" in r.text


def test_viewers_are_not_offered_2fa(web: TestClient) -> None:
    add_user(app_of(web), UserRole.VIEWER)
    login(web, email=ROLE_EMAILS[UserRole.VIEWER])
    assert "not offered" in web.get("/dashboard/account").text
    assert post(web, "/dashboard/account/2fa/start").status_code == 403


def test_new_recovery_codes_and_turning_off(web: TestClient) -> None:
    user_id, old = _enable(web, UserRole.REP)
    login(web, email=ROLE_EMAILS[UserRole.REP])
    _second_step(web, code())
    assert "10 recovery codes left" in web.get("/dashboard/account").text
    assert post(web, "/dashboard/account/2fa/recovery-codes", {"code": "000000"}).status_code == 422
    r = post(web, "/dashboard/account/2fa/recovery-codes", {"code": code(steps=1)})
    assert r.status_code == 200
    assert old[0] not in r.text
    with app_of(web).state.session_factory() as s:
        assert totp.hash_recovery(old[0]) not in set(s.scalars(select(RecoveryCode.code_hash)))
    assert post(web, "/dashboard/account/2fa/disable", {"code": "000000"}).status_code == 422
    with app_of(web).state.session_factory() as s:  # let the next step's code be reused
        user = s.get(User, user_id)
        assert user is not None
        user.totp_last_step = None
        s.commit()
    r = post(web, "/dashboard/account/2fa/disable", {"code": code()})
    assert r.headers["location"] == "/login"
    assert not _user(web, user_id).mfa_enabled
    assert web.get("/dashboard").status_code == 303  # sessions ended
    assert login(web, email=ROLE_EMAILS[UserRole.REP]).headers["location"] == "/dashboard"
    actions = _actions(web)
    assert ("mfa_recovery_regenerated", "ok") in actions
    assert ("mfa_disabled", "ok") in actions


# --------------------------------------------------------------------------- admin policy


def test_admins_must_enroll_before_using_the_dashboard(strict: TestClient) -> None:
    add_user(app_of(strict), UserRole.ADMIN)
    assert login(strict).status_code == 303  # no 2FA yet: password logs in...
    r = strict.get("/dashboard")
    assert r.headers["location"] == "/dashboard/account?setup=required"  # ...but only here
    for path in ("/dashboard/leads", "/dashboard/admin/users", "/dashboard/admin/audit"):
        assert strict.get(path).headers["location"].startswith("/dashboard/account")
    page = strict.get("/dashboard/account?setup=required").text
    assert "must turn on two-factor" in page
    r = post(strict, "/dashboard/account/2fa/start", csrf_page=ACCOUNT)
    secret = re.search(r'<code class="pick">([A-Z2-7]{32})</code>', r.text)
    assert secret
    post(strict, "/dashboard/account/2fa/confirm", {"code": code(secret.group(1))}, ACCOUNT)
    assert strict.get("/dashboard").status_code == 200
    assert "stays on" in strict.get("/dashboard/account").text
    r = post(strict, "/dashboard/account/2fa/disable", {"code": code(secret.group(1), 1)})
    assert r.status_code == 403  # required for admins


def test_reps_are_not_forced(strict: TestClient) -> None:
    add_user(app_of(strict), UserRole.REP)
    login(strict, email=ROLE_EMAILS[UserRole.REP])
    assert strict.get("/dashboard").status_code == 200


def test_admin_resets_another_users_2fa(web: TestClient) -> None:
    rep_id, _ = _enable(web, UserRole.REP)
    admin_id = add_user(app_of(web), UserRole.ADMIN)
    rep = TestClient(web.app, base_url="http://localhost", follow_redirects=False)
    login(rep, email=ROLE_EMAILS[UserRole.REP])
    _second_step(rep, code())
    assert rep.get("/dashboard").status_code == 200
    login(web)
    assert "Reset two-factor" in web.get("/dashboard/admin/users").text
    r = post(web, f"/dashboard/admin/users/{rep_id}/2fa-reset")
    assert "Two-factor reset." in web.get(r.headers["location"]).text
    assert not _user(web, rep_id).mfa_enabled
    assert rep.get("/dashboard").status_code == 303  # their session ended
    r = post(web, f"/dashboard/admin/users/{admin_id}/2fa-reset")
    assert "account page instead" in web.get(r.headers["location"]).text
    r = post(web, "/dashboard/admin/users/99999/2fa-reset")
    assert "does not exist" in web.get(r.headers["location"]).text
    assert ("mfa_reset", "ok") in _actions(web)
    with app_of(web).state.session_factory() as s:
        assert s.scalar(select(func.count(RecoveryCode.id))) == 0
        assert (
            s.scalar(
                select(func.count(UserSession.id)).where(
                    UserSession.user_id == rep_id, UserSession.revoked_at.is_(None)
                )
            )
            == 0
        )


def test_reps_cannot_reset_anyone(web: TestClient) -> None:
    rep_id, _ = _enable(web, UserRole.REP)
    other = add_user(app_of(web), UserRole.REP, email="rep2@example.com")
    login(web, email="rep2@example.com")
    assert post(web, f"/dashboard/admin/users/{rep_id}/2fa-reset").status_code == 403
    assert _user(web, rep_id).mfa_enabled
    del other


# --------------------------------------------------------------------------- demo


def test_demo_admin_uses_real_2fa_with_the_code_shown(tmp_path: Any) -> None:
    from app.config import DEFAULT_DEMO_PASSWORD

    for c in make_client(demo_mode=True, demo_leads=0, admin_2fa_required=True):
        assert "two-factor on" in c.get("/login").text
        r = login(c, password=DEFAULT_DEMO_PASSWORD)
        assert r.headers["location"].startswith("/login/2fa")
        page = c.get("/login/2fa").text
        shown = re.search(r'current code: <code class="pick">(\d{6})</code>', page)
        assert shown
        assert shown.group(1) in {code(steps=0), code(steps=1)}
        assert _second_step(c, shown.group(1)).status_code == 303
        assert c.get("/dashboard").status_code == 200
        # The shared demo accounts cannot change their 2FA, and admins cannot reset it.
        assert post(c, "/dashboard/account/2fa/start").status_code == 403
        r = post(c, "/dashboard/account/2fa/confirm", {"code": "1"})
        assert r.status_code == 403
        assert post(c, "/dashboard/account/2fa/recovery-codes", {"code": "1"}).status_code == 403
        assert post(c, "/dashboard/account/2fa/disable", {"code": "1"}).status_code == 403
        with app_of(c).state.session_factory() as s:
            rep = s.scalar(select(User).where(User.email == "rep@example.com"))
            assert rep is not None
            assert not rep.mfa_enabled
        r = post(c, f"/dashboard/admin/users/{rep.id}/2fa-reset")
        assert "turned off in the public demo" in c.get(r.headers["location"]).text
        # The rep has no 2FA, so no code is shown for them.
        c.cookies.clear()
        assert (
            login(c, email="rep@example.com", password=DEFAULT_DEMO_PASSWORD).headers["location"]
            == "/dashboard"
        )


def test_password_string_is_not_the_code(web: TestClient) -> None:
    _enable(web, UserRole.REP)
    login(web, email=ROLE_EMAILS[UserRole.REP])
    assert _second_step(web, PASSWORD).status_code == 401
