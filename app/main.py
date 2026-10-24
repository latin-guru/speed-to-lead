"""Application factory. Run with: ``uvicorn app.main:create_app --factory``."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from app import __version__
from app.ai.base import AIScorer
from app.ai.factory import build_scorer
from app.ai.resilience import CircuitBreaker
from app.ai.service import ScoringService
from app.api import health, leads, webhooks
from app.config import Settings, get_settings
from app.db import init_db, make_engine, make_session_factory
from app.logging import configure_logging
from app.security.rate_limit import TokenBucketLimiter
from app.services.leads import InlineScoringDispatcher, ScoringDispatcher
from app.web import routes as web_routes

log = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    scorer: AIScorer | None = None,
    http_client: httpx.Client | None = None,
    sleep: Callable[[float], None] | None = None,
    dispatcher: ScoringDispatcher | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, json_output=settings.log_json)

    engine = make_engine(settings.database_url)
    owned_client = http_client is None and scorer is None and settings.ai_provider != "mock"
    client = http_client or (
        httpx.Client(timeout=settings.ai_timeout_seconds) if owned_client else None
    )
    scorer = scorer or build_scorer(settings, client)
    breaker = CircuitBreaker(
        threshold=settings.ai_breaker_threshold, cooldown=settings.ai_breaker_cooldown_seconds
    )
    scoring = ScoringService(
        scorer,
        min_confidence=settings.ai_min_confidence,
        max_attempts=settings.ai_max_attempts,
        budget_seconds=settings.ai_total_budget_seconds,
        breaker=breaker,
        sleep=sleep or time.sleep,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        init_db(engine)
        app.state.ready = True
        log.info("startup", extra={"version": __version__, "ai_provider": settings.ai_provider})
        yield
        app.state.ready = False
        if owned_client and client is not None:
            client.close()
        engine.dispose()

    app = FastAPI(title=settings.app_name, version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.state.ready = False
    app.state.public_limiter = TokenBucketLimiter(
        capacity=settings.rate_limit_capacity,
        refill_per_second=settings.rate_limit_refill_per_minute / 60.0,
    )
    app.state.webhook_limiter = TokenBucketLimiter(
        capacity=settings.webhook_rate_limit_capacity,
        refill_per_second=settings.webhook_rate_limit_refill_per_minute / 60.0,
    )
    app.state.scoring_service = scoring
    app.state.scoring_dispatcher = dispatcher or InlineScoringDispatcher(scoring)

    app.include_router(health.router)
    app.include_router(leads.router)
    app.include_router(webhooks.router)
    app.include_router(web_routes.router)
    return app
