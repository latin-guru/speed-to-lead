"""Server-side dashboard sessions.

Login creates a ``user_sessions`` row and sets a cookie holding ``<random id>.<hmac>``:

- the id is 32 random bytes; the database stores only its SHA-256, so a database leak does
  not hand out live sessions;
- the HMAC (``SECRET_KEY``, domain-separated) lets the server drop forged or garbled
  cookies before touching the database;
- every login creates a **new** session (rotation: a cookie planted before login is
  useless afterwards) and revokes the one the browser had;
- a session ends at the **absolute** limit (``SESSION_TTL_HOURS``, default 12h) or after the
  **idle** limit (``SESSION_IDLE_MINUTES``, default 30) without activity, whichever comes
  first; logout, deactivating the user, changing their role or resetting their password
  revokes it on the server;
- the cookie is HttpOnly, SameSite=Strict, and Secure everywhere except plain-HTTP
  localhost. When Secure, it uses the ``__Host-`` prefix (no Domain, Path=/), so a
  subdomain cannot set or overwrite it.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import User, UserSession

SESSION_COOKIE = "stl_session"
SECURE_SESSION_COOKIE = "__Host-stl_session"
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


@dataclass(frozen=True)
class SessionPolicy:
    absolute: timedelta
    idle: timedelta
    # Write last_seen_at at most this often (not on every request).
    touch_every: timedelta = timedelta(seconds=60)


def cookie_secure(hostname: str | None) -> bool:
    return (hostname or "") not in LOCAL_HOSTS


def cookie_name(secure: bool) -> str:
    return SECURE_SESSION_COOKIE if secure else SESSION_COOKIE


def _mac(key: bytes, value: str) -> str:
    return hmac.new(key, f"session|{value}".encode(), hashlib.sha256).hexdigest()[:32]


def token_hash(session_id: str) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()


def sign(key: bytes, session_id: str) -> str:
    return f"{session_id}.{_mac(key, session_id)}"


def unsign(key: bytes, cookie: str | None) -> str | None:
    if not cookie or cookie.count(".") != 1 or len(cookie) > 200:
        return None
    session_id, mac = cookie.split(".")
    if not hmac.compare_digest(_mac(key, session_id), mac):
        return None
    return session_id


def start_session(
    db: Session,
    key: bytes,
    user: User,
    *,
    now: datetime,
    policy: SessionPolicy,
    ip_hash: str | None = None,
    user_agent: str | None = None,
) -> tuple[str, UserSession]:
    """Create a session for ``user``. Returns the cookie value and the row. Caller commits."""
    session_id = secrets.token_urlsafe(32)
    row = UserSession(
        token_hash=token_hash(session_id),
        user_id=user.id,
        created_at=now,
        last_seen_at=now,
        expires_at=now + policy.absolute,
        ip_hash=ip_hash,
        user_agent=(user_agent or "")[:200] or None,
    )
    db.add(row)
    db.flush()
    return sign(key, session_id), row


def find_session(db: Session, key: bytes, cookie: str | None) -> UserSession | None:
    """The row for ``cookie`` (valid signature), live or not."""
    session_id = unsign(key, cookie)
    if session_id is None:
        return None
    return db.scalar(select(UserSession).where(UserSession.token_hash == token_hash(session_id)))


def load_session(
    db: Session,
    key: bytes,
    cookie: str | None,
    *,
    now: datetime,
    policy: SessionPolicy,
    touch: bool = True,
) -> UserSession | None:
    """The live session for ``cookie``, or None. Refreshes the idle timer when ``touch``."""
    row = find_session(db, key, cookie)
    if row is None or row.revoked_at is not None:
        return None
    if now >= row.expires_at or now - row.last_seen_at >= policy.idle:
        return None
    if not row.user.active:
        return None
    if touch and now - row.last_seen_at >= policy.touch_every:
        row.last_seen_at = now
        db.commit()
    return row


def revoke(db: Session, row: UserSession, *, now: datetime) -> None:
    if row.revoked_at is None:
        row.revoked_at = now


def revoke_all(db: Session, user_id: int, *, now: datetime) -> int:
    """End every live session of a user (deactivated, role changed, password reset)."""
    result = db.execute(
        update(UserSession)
        .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    return int(getattr(result, "rowcount", 0) or 0)
