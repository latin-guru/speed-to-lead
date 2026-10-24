"""Two-factor (TOTP) enrollment, verification, recovery codes and resets.

Policy: required for admins (``ADMIN_2FA_REQUIRED``, default on; an admin without it can
only reach the enrollment page), optional for reps, not offered to viewers. A wrong code
counts as a failed login on the same counter as a wrong password, so the account lockout
applies to code guessing too.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import RecoveryCode, User, UserRole
from app.security import fields, totp
from app.security.sessions import revoke_all
from app.services.users import LoginPolicy, register_failure

ENROLLABLE = frozenset({UserRole.ADMIN.value, UserRole.REP.value})


class MFAError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class Method(enum.StrEnum):
    TOTP = "totp"
    RECOVERY = "recovery"


@dataclass(frozen=True)
class SecondFactor:
    ok: bool
    method: Method | None = None
    locked: bool = False
    remaining_codes: int | None = None


def can_enroll(user: User) -> bool:
    return user.role in ENROLLABLE


def required_for(user: User, *, admin_required: bool) -> bool:
    return admin_required and user.role == UserRole.ADMIN.value


def begin_enrollment(user: User) -> str:
    """Start (or restart) enrollment: a new pending secret. The caller commits."""
    if not can_enroll(user):
        raise MFAError("not_allowed", "two-factor is not available for this role")
    secret = totp.new_secret()
    user.totp_pending_enc = fields.active().encrypt(secret)
    return secret


def pending_secret(user: User) -> str | None:
    return fields.active().decrypt(user.totp_pending_enc) if user.totp_pending_enc else None


def active_secret(user: User) -> str | None:
    return fields.active().decrypt(user.totp_secret_enc) if user.totp_secret_enc else None


def _replace_recovery_codes(session: Session, user: User, now: datetime) -> list[str]:
    session.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user.id))
    codes = totp.new_recovery_codes()
    session.add_all(
        RecoveryCode(user_id=user.id, code_hash=totp.hash_recovery(c), created_at=now)
        for c in codes
    )
    return codes


def confirm_enrollment(session: Session, user: User, code: str, *, now: datetime) -> list[str]:
    """Activate the pending secret if ``code`` matches it. Returns new recovery codes."""
    secret = pending_secret(user)
    if secret is None:
        raise MFAError("no_pending", "start the set-up again")
    step = totp.verify(secret, code, now=now, last_step=None)
    if step is None:
        raise MFAError("bad_code", "that code did not match; check the time on your phone")
    user.totp_secret_enc = fields.active().encrypt(secret)
    user.totp_pending_enc = None
    user.totp_enabled_at = now
    user.totp_last_step = step
    return _replace_recovery_codes(session, user, now)


def enable_with_secret(session: Session, user: User, secret: str, *, now: datetime) -> list[str]:
    """Turn two-factor on with a known secret (demo seeding and tests)."""
    user.totp_secret_enc = fields.active().encrypt(secret)
    user.totp_pending_enc = None
    user.totp_enabled_at = now
    user.totp_last_step = None
    return _replace_recovery_codes(session, user, now)


def check_current_code(user: User, code: str, *, now: datetime) -> bool:
    """For sensitive account changes (new recovery codes, turning 2FA off)."""
    secret = active_secret(user)
    step = totp.verify(secret, code, now=now, last_step=user.totp_last_step) if secret else None
    if step is None:
        return False
    user.totp_last_step = step
    return True


def remaining_recovery_codes(session: Session, user: User) -> int:
    return len(
        list(
            session.scalars(
                select(RecoveryCode.id).where(
                    RecoveryCode.user_id == user.id, RecoveryCode.used_at.is_(None)
                )
            )
        )
    )


def verify_second_factor(
    session: Session, user: User, code: str, *, now: datetime, policy: LoginPolicy
) -> SecondFactor:
    """The login step after the password. Updates the failure count; the caller commits."""
    if user.locked_until is not None and user.locked_until > now:
        return SecondFactor(False, locked=True)
    secret = active_secret(user)
    method: Method | None = None
    if secret is not None and not totp.looks_like_recovery_code(code):
        step = totp.verify(secret, code[:16], now=now, last_step=user.totp_last_step)
        if step is not None:
            user.totp_last_step = step
            method = Method.TOTP
    elif secret is not None:
        row = session.scalar(
            select(RecoveryCode).where(
                RecoveryCode.user_id == user.id,
                RecoveryCode.code_hash == totp.hash_recovery(code[:32]),
                RecoveryCode.used_at.is_(None),
            )
        )
        if row is not None:
            row.used_at = now
            method = Method.RECOVERY
    if method is None:
        locked_now = register_failure(user, now=now, policy=policy)
        return SecondFactor(False, locked=locked_now)
    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = now
    remaining = remaining_recovery_codes(session, user) if method is Method.RECOVERY else None
    return SecondFactor(True, method, remaining_codes=remaining)


def regenerate_recovery_codes(session: Session, user: User, *, now: datetime) -> list[str]:
    if not user.mfa_enabled:
        raise MFAError("not_enabled", "two-factor is not on")
    return _replace_recovery_codes(session, user, now)


def disable(session: Session, user: User, *, now: datetime) -> None:
    """Turn two-factor off (a rep doing it themselves, or an admin resetting someone).
    Ends the user's sessions."""
    user.totp_secret_enc = None
    user.totp_pending_enc = None
    user.totp_enabled_at = None
    user.totp_last_step = None
    session.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user.id))
    revoke_all(session, user.id, now=now)
