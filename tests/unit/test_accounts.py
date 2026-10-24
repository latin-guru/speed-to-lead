"""Users, passwords, sessions, permissions and audit helpers (no HTTP)."""

import io
import uuid
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import cli
from app.db import init_db, make_engine, make_session_factory
from app.models import AuditEvent, Lead, User, UserRole, utcnow
from app.security import passwords
from app.security.authz import ROLE_PERMISSIONS, Permission, Principal, can_on_lead, has
from app.security.sessions import SessionPolicy, load_session, revoke_all, start_session, unsign
from app.services import audit
from app.services.users import (
    AuthOutcome,
    LoginPolicy,
    UserError,
    authenticate,
    create_user,
    normalize_email,
    set_password,
    unlock,
    update_user,
)
from tests.conftest import PASSWORD, make_settings

KEY = b"unit-test-session-key"


@pytest.fixture
def db() -> Iterator[Session]:
    engine = make_engine("sqlite://")
    init_db(engine)
    with make_session_factory(engine)() as s:
        yield s
    engine.dispose()


# --------------------------------------------------------------------------- passwords


def test_password_policy_and_hashing() -> None:
    with pytest.raises(passwords.PasswordPolicyError, match="at least 12"):
        passwords.check_policy("short")
    with pytest.raises(passwords.PasswordPolicyError, match="at most 128"):
        passwords.check_policy("x" * 129)
    with pytest.raises(passwords.PasswordPolicyError, match="email"):
        passwords.check_policy("Lee@Example.com ", email="lee@example.com")
    passwords.check_policy(PASSWORD, email="lee@example.com")
    hashed = passwords.hash_password(PASSWORD)
    assert hashed.startswith("$argon2id$")
    assert PASSWORD not in hashed
    assert passwords.verify_password(hashed, PASSWORD)
    assert not passwords.verify_password(hashed, "nope")
    assert not passwords.verify_password(hashed, "x" * 500)  # over the cap: never hashed
    assert not passwords.verify_password("not-a-hash", PASSWORD)
    assert passwords.needs_rehash("not-a-hash")
    assert not passwords.needs_rehash(hashed)
    passwords.dummy_verify()  # no error, no result


# --------------------------------------------------------------------------- users


def test_create_user_validates(db: Session) -> None:
    user = create_user(db, " Ana@Example.COM ", PASSWORD, "rep")
    assert user.email == "ana@example.com"
    assert user.actor_name == "ana"
    with pytest.raises(UserError, match="already exists"):
        create_user(db, "ana@example.com", PASSWORD, UserRole.REP)
    with pytest.raises(UserError, match="valid email"):
        normalize_email("nope")
    with pytest.raises(UserError, match="admin, rep or viewer"):
        create_user(db, "b@example.com", PASSWORD, "root")
    with pytest.raises(UserError, match="at least 12"):
        create_user(db, "b@example.com", "short", "rep")
    odd = User(id=77, email="+++@example.com", password_hash="x", role="rep")
    assert odd.actor_name == "user77"


def test_authenticate_outcomes(db: Session) -> None:
    now = utcnow()
    policy = LoginPolicy(max_failures=2, lockout_base=timedelta(seconds=10))
    create_user(db, "lee@example.com", PASSWORD, "admin")
    assert authenticate(db, "x" * 300, PASSWORD, now=now, policy=policy).outcome is (
        AuthOutcome.UNKNOWN_USER
    )
    assert authenticate(db, "nobody@example.com", PASSWORD, now=now, policy=policy).outcome is (
        AuthOutcome.UNKNOWN_USER
    )
    first = authenticate(db, "lee@example.com", "wrong", now=now, policy=policy)
    assert (first.outcome, first.locked_now) == (AuthOutcome.BAD_PASSWORD, False)
    second = authenticate(db, "LEE@example.com", "wrong", now=now, policy=policy)
    assert second.locked_now
    locked = authenticate(db, "lee@example.com", PASSWORD, now=now, policy=policy)
    assert locked.outcome is AuthOutcome.LOCKED
    later = now + timedelta(seconds=11)
    ok = authenticate(db, "lee@example.com", PASSWORD, now=later, policy=policy)
    assert ok.ok
    assert ok.user is not None
    assert ok.user.failed_logins == 0
    ok.user.active = False
    assert authenticate(db, "lee@example.com", PASSWORD, now=later, policy=policy).outcome is (
        AuthOutcome.INACTIVE
    )


def test_lockout_backoff_is_capped() -> None:
    policy = LoginPolicy(
        max_failures=3, lockout_base=timedelta(seconds=30), lockout_max=timedelta(minutes=5)
    )
    assert policy.lockout(3) == timedelta(seconds=30)
    assert policy.lockout(4) == timedelta(seconds=60)
    assert policy.lockout(5) == timedelta(seconds=120)
    assert policy.lockout(50) == timedelta(minutes=5)


def test_update_user_rules(db: Session) -> None:
    now = utcnow()
    admin = create_user(db, "a@example.com", PASSWORD, "admin")
    rep = create_user(db, "r@example.com", PASSWORD, "rep")
    assert update_user(db, rep, acting_user_id=admin.id, role="viewer", now=now) == {
        "role": ["rep", "viewer"]
    }
    assert update_user(db, rep, acting_user_id=admin.id, role="viewer", active=True, now=now) == {}
    with pytest.raises(UserError, match="own role"):
        update_user(db, admin, acting_user_id=admin.id, role="rep", now=now)
    with pytest.raises(UserError, match="own role"):
        update_user(db, admin, acting_user_id=admin.id, active=False, now=now)
    with pytest.raises(UserError, match="at least one active admin"):
        update_user(db, admin, acting_user_id=None, active=False, now=now)
    with pytest.raises(UserError, match="admin, rep or viewer"):
        update_user(db, rep, acting_user_id=admin.id, role="boss", now=now)
    # Same role for yourself is not a change.
    assert update_user(db, admin, acting_user_id=admin.id, role="admin", active=True, now=now) == {}


def test_set_password_and_unlock(db: Session) -> None:
    now = utcnow()
    user = create_user(db, "p@example.com", PASSWORD, "rep")
    user.failed_logins, user.locked_until = 9, now + timedelta(hours=1)
    with pytest.raises(UserError, match="at least 12"):
        set_password(db, user, "short", now=now)
    set_password(db, user, "another long password", now=now)
    assert user.locked_until is None
    assert passwords.verify_password(user.password_hash, "another long password")
    assert not unlock(user)


# --------------------------------------------------------------------------- sessions


def test_sessions_idle_absolute_revocation(db: Session) -> None:
    now = utcnow()
    user = create_user(db, "s@example.com", PASSWORD, "rep")
    policy = SessionPolicy(absolute=timedelta(hours=12), idle=timedelta(minutes=30))
    cookie, row = start_session(db, KEY, user, now=now, policy=policy, user_agent="x" * 500)
    db.commit()
    assert row.user_agent is not None
    assert len(row.user_agent) == 200
    assert load_session(db, KEY, cookie, now=now, policy=policy) is row
    assert load_session(db, b"another-key-entirely", cookie, now=now, policy=policy) is None
    assert load_session(db, KEY, cookie, now=now + timedelta(minutes=31), policy=policy) is None
    assert load_session(db, KEY, cookie, now=now + timedelta(minutes=10), policy=policy) is row
    assert unsign(KEY, None) is None
    assert unsign(KEY, "a" * 201) is None
    assert revoke_all(db, user.id, now=now) == 1
    assert load_session(db, KEY, cookie, now=now, policy=policy) is None
    assert revoke_all(db, user.id, now=now) == 0


# --------------------------------------------------------------------------- permissions


def test_permission_table() -> None:
    assert ROLE_PERMISSIONS[UserRole.ADMIN] == frozenset(Permission)
    viewer = Principal(
        kind="user", role=UserRole.VIEWER, actor="human:v", audit_actor="v", user_id=3
    )
    rep = Principal(kind="user", role=UserRole.REP, actor="human:r", audit_actor="r", user_id=2)
    token = Principal.api_token()
    assert token.name == "api-token"
    assert has(token, Permission.MANAGE_USERS)
    assert not has(rep, Permission.MANAGE_USERS)
    assert not has(viewer, Permission.CHANGE_STAGE)
    lead = Lead(id=uuid.uuid4(), name="x", source="api", assignee_id=2)
    assert can_on_lead(rep, Permission.REVEAL_CONTACT, lead)
    assert can_on_lead(rep, Permission.VIEW_LEADS, lead)
    assert not can_on_lead(rep, Permission.ASSIGN_SELF, lead)
    assert can_on_lead(viewer, Permission.VIEW_LEADS, lead)  # viewers read
    lead.assignee_id = None
    assert can_on_lead(rep, Permission.ASSIGN_SELF, lead)
    assert not can_on_lead(rep, Permission.REVEAL_CONTACT, lead)
    assert can_on_lead(token, Permission.REVEAL_CONTACT, lead)


# --------------------------------------------------------------------------- audit


def test_audit_summary_is_scrubbed_and_capped(db: Session) -> None:
    event = audit.record(
        db,
        actor="cli",
        role=None,
        action="x" * 80,
        target_id=uuid.UUID(int=1),
        summary={
            "phone": "(202) 555-0101",
            "nested": {"deep": {"deeper": {"email": "a@example.com"}}},
            "items": ["b@example.com", 1, 2.5, None, True],
            "set": {"c@example.com"},
            "obj": object(),
            "long": "y" * 500,
            **{f"k{i}": i for i in range(30)},
        },
    )
    db.commit()
    s = event.summary
    assert s["phone"] == "***-***-0101"
    assert "a@example.com" not in str(s["nested"])
    assert s["items"][0] == "b***@example.com"
    assert s["set"] == ["c***@example.com"]
    assert isinstance(s["obj"], str)
    assert len(s["long"]) == 200
    assert len(s) == 20
    assert len(event.action) == 48
    assert audit.ip_hash(KEY, None) is None
    assert audit.ip_hash(KEY, "203.0.113.9") != "203.0.113.9"


# --------------------------------------------------------------------------- CLI


def _cli_db(tmp_path: Path) -> str:
    url = f"sqlite:///{tmp_path / 'users.db'}"
    engine = make_engine(url)
    init_db(engine)
    engine.dispose()
    return url


def test_cli_create_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_settings(database_url=_cli_db(tmp_path))
    out = io.StringIO()
    args = ["create-user", "--email", "ops@example.com", "--role", "admin", "--password-stdin"]
    assert cli.main(args, settings=settings, out=out, stdin=io.StringIO(PASSWORD + "\n")) == 0
    assert "created admin ops@example.com" in out.getvalue()
    out = io.StringIO()
    assert cli.main(args, settings=settings, out=out, stdin=io.StringIO(PASSWORD + "\n")) == 1
    assert "already exists" in out.getvalue()
    # Interactive: two prompts that must match.
    answers = iter([PASSWORD, "something else entirely"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: next(answers))
    out = io.StringIO()
    assert cli.main(args[:-1], settings=settings, out=out) == 1
    assert "did not match" in out.getvalue()
    answers = iter([PASSWORD, PASSWORD])
    out = io.StringIO()
    rep_args = ["create-user", "--email", "rep9@example.com", "--role", "rep"]
    assert cli.main(rep_args, settings=settings, out=out) == 0
    engine = make_engine(settings.database_url)
    with make_session_factory(engine)() as s:
        assert {u.email for u in s.scalars(select(User))} == {"ops@example.com", "rep9@example.com"}
        assert [e.actor for e in s.scalars(select(AuditEvent))] == ["cli", "cli"]
    engine.dispose()
