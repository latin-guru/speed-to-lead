"""Shared FastAPI dependencies: DB session, client IP, rate limits."""

from __future__ import annotations

import hmac
import time
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import Settings
from app.security.rate_limit import TokenBucketLimiter
from app.security.session import SESSION_COOKIE, DashboardSession, Role, read_session


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


def require_admin(request: Request) -> None:
    """Bearer-token check for operator endpoints. Fails closed (503) when no token is set."""
    settings: Settings = request.app.state.settings
    expected = settings.admin_token.get_secret_value() if settings.admin_token else ""
    if not expected:
        raise HTTPException(status_code=503, detail="admin API not configured (ADMIN_TOKEN)")
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(
            status_code=401, detail="invalid admin token", headers={"WWW-Authenticate": "Bearer"}
        )


AdminDep = Depends(require_admin)


def session_tokens(settings: Settings) -> dict[Role, str | None]:
    """The token each dashboard role logs in with. ``demo`` only exists in demo mode."""
    admin = settings.admin_token.get_secret_value() if settings.admin_token else None
    demo = None
    if settings.demo_mode and settings.demo_token is not None:
        demo = settings.demo_token.get_secret_value() or None
    return {"admin": admin or None, "demo": demo}


def dashboard_session(request: Request) -> DashboardSession | None:
    settings: Settings = request.app.state.settings
    return read_session(
        settings.secret_key_bytes,
        request.cookies.get(SESSION_COOKIE),
        session_tokens(settings),
        now=time.time(),
    )
