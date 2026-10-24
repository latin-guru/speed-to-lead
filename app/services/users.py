"""Dashboard accounts: create, authenticate (with lockout), and admin changes.

Login failures are counted per account. After ``max_failures`` in a row the account locks
for ``lockout_base`` seconds, doubling with every further failure after a lock ends, up to
``lockout_max``. While locked, even the right password is refused (and the password is not
checked, so guessing does not continue during the lock); a successful login resets the
count. Every failure mode returns the same generic result to the caller.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime, timedelta

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import User, UserRole
from app.security import passwords
from app.security.sessions import revoke_all


class UserError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class AuthOutcome(enum.StrEnum):
    OK = "ok"
    UNKNOWN_USER = "unknown_user"
    BAD_PASSWORD = "bad_password"  # noqa: S105  (an outcome name, not a password)
    LOCKED = "locked"
    INACTIVE = "inactive"


@dataclass(frozen=True)
class LoginPolicy:
    max_failures: int = 5
    lockout_base: timedelta = timedelta(seconds=60)
    lockout_max: timedelta = timedelta(hours=1)

    def lockout(self, failures: int) -> timedelta:
        """Lock length after ``failures`` consecutive failures (>= max_failures)."""
        exponent = min(max(0, failures - self.max_failures), 20)
        length: timedelta = self.lockout_base * (2**exponent)
        return min(self.lockout_max, length)


@dataclass(frozen=True)
class AuthResult:
    outcome: AuthOutcome
    user: User | None = None
    locked_now: bool = False  # this failure locked the account

    @property
    def ok(self) -> bool:
        return self.outcome is AuthOutcome.OK


def normalize_email(email: str) -> str:
    try:
        return validate_email(email.strip(), check_deliverability=False).normalized.lower()
    except EmailNotValidError as exc:
        raise UserError("invalid_email", "enter a valid email address") from exc


def find_by_email(session: Session, email: str) -> User | None:
    return session.scalar(select(User).where(User.email == email.strip().lower()))


def create_user(session: Session, email: str, password: str, role: UserRole | str) -> User:
    """Add a user (the caller commits). Raises ``UserError`` for bad input or a duplicate."""
    address = normalize_email(email)
    try:
        role = UserRole(role)
    except ValueError as exc:
        raise UserError("invalid_role", "role must be admin, rep or viewer") from exc
    try:
        passwords.check_policy(password, email=address)
    except passwords.PasswordPolicyError as exc:
        raise UserError("weak_password", str(exc)) from exc
    if find_by_email(session, address) is not None:
        raise UserError("duplicate", "a user with that email already exists")
    user = User(
        email=address,
        password_hash=passwords.hash_password(password),
        role=role.value,
        active=True,
        failed_logins=0,
    )
    session.add(user)
    session.flush()
    return user


def authenticate(
    session: Session, email: str, password: str, *, now: datetime, policy: LoginPolicy
) -> AuthResult:
    """Check a login. Updates the failure count / lock on the user; the caller commits."""
    user = find_by_email(session, email) if len(email) <= 254 else None
    if user is None:
        passwords.dummy_verify()
        return AuthResult(AuthOutcome.UNKNOWN_USER)
    if user.locked_until is not None and user.locked_until > now:
        passwords.dummy_verify()
        return AuthResult(AuthOutcome.LOCKED, user)
    matched = passwords.verify_password(user.password_hash, password)
    if not user.active:
        return AuthResult(AuthOutcome.INACTIVE, user)
    if not matched:
        user.failed_logins += 1
        locked_now = user.failed_logins >= policy.max_failures
        if locked_now:
            user.locked_until = now + policy.lockout(user.failed_logins)
        return AuthResult(AuthOutcome.BAD_PASSWORD, user, locked_now=locked_now)
    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = now
    if passwords.needs_rehash(user.password_hash):
        user.password_hash = passwords.hash_password(password)
    return AuthResult(AuthOutcome.OK, user)


def active_admins(session: Session) -> int:
    return int(
        session.scalar(
            select(func.count(User.id)).where(
                User.role == UserRole.ADMIN.value, User.active.is_(True)
            )
        )
        or 0
    )


def update_user(
    session: Session,
    user: User,
    *,
    acting_user_id: int | None,
    role: UserRole | str | None = None,
    active: bool | None = None,
    now: datetime,
) -> dict[str, object]:
    """Change role and/or active flag. Returns ``{"field": [before, after]}`` for the audit.

    An admin cannot change their own role or deactivate themselves, and the last active
    admin cannot be removed, so the system can never be left without an admin. Any change
    ends the user's sessions (their permissions changed).
    """
    changes: dict[str, object] = {}
    new_role = None
    if role is not None:
        try:
            new_role = UserRole(role)
        except ValueError as exc:
            raise UserError("invalid_role", "role must be admin, rep or viewer") from exc
    if user.id == acting_user_id and (
        (new_role is not None and new_role.value != user.role)
        or (active is not None and active != user.active)
    ):
        raise UserError("self_change", "you cannot change your own role or deactivate yourself")
    losing_admin = user.role == UserRole.ADMIN.value and user.active
    removes_admin = (new_role is not None and new_role is not UserRole.ADMIN) or active is False
    if losing_admin and removes_admin and active_admins(session) <= 1:
        raise UserError("last_admin", "keep at least one active admin")
    if new_role is not None and new_role.value != user.role:
        changes["role"] = [user.role, new_role.value]
        user.role = new_role.value
    if active is not None and active != user.active:
        changes["active"] = [user.active, active]
        user.active = active
    if changes:
        revoke_all(session, user.id, now=now)
    return changes


def unlock(user: User) -> bool:
    was_locked = user.failed_logins > 0 or user.locked_until is not None
    user.failed_logins = 0
    user.locked_until = None
    return was_locked


def set_password(session: Session, user: User, password: str, *, now: datetime) -> None:
    try:
        passwords.check_policy(password, email=user.email)
    except passwords.PasswordPolicyError as exc:
        raise UserError("weak_password", str(exc)) from exc
    user.password_hash = passwords.hash_password(password)
    unlock(user)
    revoke_all(session, user.id, now=now)
