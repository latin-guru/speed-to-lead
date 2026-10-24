"""TOTP primitives and the two-factor service's edge cases."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.db import init_db, make_engine, make_session_factory
from app.models import User, UserRole
from app.security import totp
from app.services import mfa
from app.services.users import LoginPolicy

NOW = datetime(2026, 3, 2, 15, 0, 7, tzinfo=UTC)
SECRET = totp.new_secret()


def test_codes_drift_and_replay() -> None:
    now_code = totp.current_code(SECRET, NOW)
    step = totp.step_of(NOW)
    assert totp.verify(SECRET, now_code, now=NOW, last_step=None) == step
    assert totp.verify(SECRET, f" {now_code[:3]} {now_code[3:]} ", now=NOW, last_step=None) == step
    assert totp.verify(SECRET, now_code, now=NOW, last_step=step) is None  # replay
    previous = totp.current_code(SECRET, NOW - timedelta(seconds=30))
    assert totp.verify(SECRET, previous, now=NOW, last_step=None) == step - 1  # drift
    too_old = totp.current_code(SECRET, NOW - timedelta(seconds=90))
    assert totp.verify(SECRET, too_old, now=NOW, last_step=None) is None
    for bad in ("", "12345", "1234567", "abcdef"):
        assert totp.verify(SECRET, bad, now=NOW, last_step=None) is None


def test_recovery_code_format_and_hashing() -> None:
    codes = totp.new_recovery_codes()
    assert len(codes) == 10
    assert all(len(c) == 11 and c[5] == "-" for c in codes)
    assert totp.hash_recovery("ABCDE-FGHJK") == totp.hash_recovery("abcde fghjk")
    assert totp.looks_like_recovery_code("abcde-fghjk")
    assert not totp.looks_like_recovery_code("123456")
    uri = totp.provisioning_uri(SECRET, account="rep@example.com", issuer="speed-to-lead")
    assert uri.startswith("otpauth://totp/")
    assert totp.qr_svg_data_uri(uri).startswith("data:image/svg+xml")
    assert totp.demo_secret() == totp.demo_secret()


@pytest.fixture
def db() -> Iterator[Session]:
    engine = make_engine("sqlite://")
    init_db(engine)
    with make_session_factory(engine)() as s:
        yield s
    engine.dispose()


def _user(db: Session, role: UserRole) -> User:
    user = User(email=f"{role.value}@example.com", password_hash="x", role=role.value)
    db.add(user)
    db.flush()
    return user


def test_service_edge_cases(db: Session) -> None:
    viewer = _user(db, UserRole.VIEWER)
    with pytest.raises(mfa.MFAError, match="not available"):
        mfa.begin_enrollment(viewer)
    rep = _user(db, UserRole.REP)
    with pytest.raises(mfa.MFAError, match="not on"):
        mfa.regenerate_recovery_codes(db, rep, now=NOW)
    assert not mfa.check_current_code(rep, "123456", now=NOW)
    assert mfa.pending_secret(rep) is None
    # No secret at all: the second step can only fail.
    result = mfa.verify_second_factor(db, rep, "123456", now=NOW, policy=LoginPolicy())
    assert not result.ok
    assert rep.failed_logins == 1
    assert mfa.required_for(_user(db, UserRole.ADMIN), admin_required=True)
    assert not mfa.required_for(rep, admin_required=True)
