"""Shared FastAPI dependencies: DB session, client IP, rate limits."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import Settings
from app.security.rate_limit import TokenBucketLimiter


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
