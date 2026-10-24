"""Invite links for Interview mode: create, look up, use once, revoke.

A token is 32 random bytes (URL-safe). Only ``sha256("invite|" + token)`` is stored, so a
copy of the database holds no usable link, and the plain token is shown exactly once, to
the person who created it. Using an invite is a single conditional UPDATE, so two
simultaneous submissions cannot both win.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import InterviewInvite

TOKEN_BYTES = 32
LABEL_MAX = 80
# Rough shape of a token from secrets.token_urlsafe(32): 43 URL-safe characters.
TOKEN_MIN, TOKEN_MAX = 20, 100


def hash_token(token: str, purpose: str = "invite") -> str:
    return hashlib.sha256(f"{purpose}|{token}".encode()).hexdigest()


def plausible_token(token: str) -> bool:
    return TOKEN_MIN <= len(token) <= TOKEN_MAX and all(
        c.isascii() and (c.isalnum() or c in "-_") for c in token
    )


def clean_label(raw: str | None) -> str | None:
    label = " ".join((raw or "").split())[:LABEL_MAX]
    return label or None


@dataclass(frozen=True)
class NewInvite:
    invite: InterviewInvite
    token: str  # shown once; never stored


def create_invite(
    session: Session,
    *,
    created_by: str,
    label: str | None,
    ttl: timedelta,
    now: datetime,
) -> NewInvite:
    token = secrets.token_urlsafe(TOKEN_BYTES)
    invite = InterviewInvite(
        token_hash=hash_token(token),
        label=clean_label(label),
        created_by=created_by[:254],
        created_at=now,
        expires_at=now + ttl,
    )
    session.add(invite)
    session.flush()
    return NewInvite(invite, token)


def find_by_token(session: Session, token: str) -> InterviewInvite | None:
    if not plausible_token(token):
        return None
    return session.scalar(
        select(InterviewInvite).where(InterviewInvite.token_hash == hash_token(token))
    )


def claim(session: Session, invite: InterviewInvite, *, now: datetime) -> bool:
    """Mark ``invite`` used, if it still is active. True if this call won it."""
    result = session.execute(
        update(InterviewInvite)
        .where(
            InterviewInvite.id == invite.id,
            InterviewInvite.used_at.is_(None),
            InterviewInvite.revoked_at.is_(None),
            InterviewInvite.expires_at > now,
        )
        .values(used_at=now)
        .execution_options(synchronize_session=False)
    )
    won = getattr(result, "rowcount", 0) == 1
    if won:
        session.refresh(invite)
    return won


def revoke(session: Session, invite: InterviewInvite, *, now: datetime) -> bool:
    """Revoke an invite that has not been used. True if it changed."""
    if invite.revoked_at is not None or invite.used_at is not None:
        return False
    invite.revoked_at = now
    return True


def new_viewer_token() -> tuple[str, str]:
    """A random token for the visitor's browser (cookie) and its stored hash."""
    token = secrets.token_urlsafe(TOKEN_BYTES)
    return token, hash_token(token, "viewer")


def viewer_matches(invite: InterviewInvite, cookie: str | None) -> bool:
    if not cookie or invite.viewer_hash is None or len(cookie) > TOKEN_MAX:
        return False
    return hmac.compare_digest(hash_token(cookie, "viewer"), invite.viewer_hash)


def list_invites(session: Session, *, limit: int = 100) -> list[InterviewInvite]:
    stmt = select(InterviewInvite).order_by(InterviewInvite.id.desc()).limit(limit)
    return list(session.scalars(stmt))
