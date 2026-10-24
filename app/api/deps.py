"""Shared FastAPI dependencies: DB session, client IP, rate limits, who is calling."""

from __future__ import annotations

import hmac
from collections.abc import Iterator
from datetime import timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import UserRole, utcnow
from app.security.authz import Principal
from app.security.rate_limit import TokenBucketLimiter
from app.security.sessions import (
    SessionPolicy,
    cookie_name,
    cookie_secure,
    load_session,
)
from app.services.pipeline import HUMAN_PREFIX


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


def client_ip(request: Request) -> str:
    settings: Settings = request.app.state.settings
    if settings.trust_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for", "")
        hops = [h.strip() for h in forwarded.split(",") if h.strip()]
        if hops:
            return hops[-1]  # right-most hop was appended by our own proxy
    return request.client.host if request.client else "unknown"


def too_many(retry_after: int, detail: str = "rate limit exceeded") -> HTTPException:
    return HTTPException(status_code=429, detail=detail, headers={"Retry-After": str(retry_after)})


def _enforce(limiter: TokenBucketLimiter, request: Request, scope: str) -> None:
    decision = limiter.hit(f"{scope}:{client_ip(request)}")
    if not decision.allowed:
        raise too_many(decision.retry_after_seconds)


def public_rate_limit(request: Request) -> None:
    _enforce(request.app.state.public_limiter, request, "public")


def webhook_rate_limit(request: Request) -> None:
    _enforce(request.app.state.webhook_limiter, request, "webhook")


def login_rate_limit(request: Request) -> None:
    _enforce(request.app.state.login_limiter, request, "login")


def require_admin(request: Request) -> Principal:
    """Bearer ``ADMIN_TOKEN`` for the machine API. Fails closed (503) when no token is set.

    The token is for scripts and integrations. People use the dashboard with their own
    account; a dashboard session is not accepted here (except on ``/api/metrics``).
    """
    settings: Settings = request.app.state.settings
    expected = settings.admin_token.get_secret_value() if settings.admin_token else ""
    if not expected:
        raise HTTPException(status_code=503, detail="admin API not configured (ADMIN_TOKEN)")
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(
            status_code=401, detail="invalid admin token", headers={"WWW-Authenticate": "Bearer"}
        )
    return Principal.api_token()


AdminDep = Depends(require_admin)
TokenPrincipal = Annotated[Principal, Depends(require_admin)]

# The dashboard's background refresh sends this header, so polling does not count as
# activity and an unattended tab still reaches the idle timeout.
BACKGROUND_HEADER = "x-background-refresh"


def session_policy(settings: Settings) -> SessionPolicy:
    return SessionPolicy(
        absolute=timedelta(hours=settings.session_ttl_hours),
        idle=timedelta(minutes=settings.session_idle_minutes),
    )


def session_cookie(request: Request) -> str | None:
    return request.cookies.get(cookie_name(cookie_secure(request.url.hostname)))


def dashboard_principal(request: Request, db: Session) -> Principal | None:
    """The logged-in dashboard user for this request, or None."""
    settings: Settings = request.app.state.settings
    row = load_session(
        db,
        settings.secret_key_bytes,
        session_cookie(request),
        now=utcnow(),
        policy=session_policy(settings),
        touch=request.headers.get(BACKGROUND_HEADER) != "1",
    )
    if row is None:
        return None
    user = row.user
    return Principal(
        kind="user",
        role=UserRole(user.role),
        actor=HUMAN_PREFIX + user.actor_name,
        audit_actor=user.email,
        user_id=user.id,
        session_id=row.id,
        session_hash=row.token_hash,
    )
