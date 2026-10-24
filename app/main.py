"""Application factory. Run with: ``uvicorn app.main:create_app --factory``.

The API only captures and enqueues. Scoring, follow-ups and CRM sync run in the worker
(``python -m app.worker``).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.ai.base import AIScorer
from app.api import admin, health, inbound, leads, metrics, tasks, webhooks
from app.config import Settings, get_settings
from app.crm.base import CRMAdapter
from app.db import init_db, make_engine, make_session_factory, schema_is_current
from app.demo.runtime import DemoRunner, reseed
from app.followups.sequence import Sequence
from app.logging import configure_logging
from app.runtime import build_job_context, build_scoring
from app.security.rate_limit import TokenBucketLimiter
from app.services.leads import Intake
from app.web import dashboard
from app.web import routes as web_routes

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "web" / "static"


def create_app(
    settings: Settings | None = None,
    *,
    scorer: AIScorer | None = None,
    http_client: httpx.Client | None = None,
    sleep: Callable[[float], None] | None = None,
    sequence: Sequence | None = None,
    crm: CRMAdapter | None = None,
    demo_simulate: bool = True,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, json_output=settings.log_json)

    engine = make_engine(settings.database_url)
    owned_client = http_client is None and scorer is None and settings.ai_provider != "mock"
    client = http_client or (
        httpx.Client(timeout=settings.ai_timeout_seconds) if owned_client else None
    )
    # The API process does not score, but it builds the same context as the worker so a
    # misconfigured provider fails at startup here too, and tests can run jobs in-process.
    scoring = build_scoring(settings, scorer=scorer, http_client=client, sleep=sleep)
    job_context = build_job_context(settings, scoring=scoring, sequence=sequence, crm=crm)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        runner: DemoRunner | None = None
        if settings.demo_mode:
            # Fresh fake data on every start, then the worker and simulator in-process.
            reseed(settings, engine, app.state.session_factory)
            app.state.schema_ok = True
            runner = DemoRunner(
                settings, app.state.session_factory, job_context, simulate=demo_simulate
            )
            runner.start()
        elif settings.environment == "test":
            init_db(engine)  # tests build the schema from the models
            app.state.schema_ok = True
        else:
            app.state.schema_ok = schema_is_current(engine)
            if not app.state.schema_ok:
                log.error("schema_not_current", extra={"hint": "run: alembic upgrade head"})
        app.state.ready = True
        log.info("startup", extra={"version": __version__, "ai_provider": settings.ai_provider})
        yield
        app.state.ready = False
        if runner is not None:
            runner.shutdown()
        if owned_client and client is not None:
            client.close()
        engine.dispose()

    app = FastAPI(title=settings.app_name, version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.state.ready = False
    app.state.schema_ok = False
    app.state.public_limiter = TokenBucketLimiter(
        capacity=settings.rate_limit_capacity,
        refill_per_second=settings.rate_limit_refill_per_minute / 60.0,
    )
    app.state.webhook_limiter = TokenBucketLimiter(
        capacity=settings.webhook_rate_limit_capacity,
        refill_per_second=settings.webhook_rate_limit_refill_per_minute / 60.0,
    )
    app.state.scoring_service = scoring
    app.state.job_context = job_context
    app.state.intake = Intake(
        sequence=job_context.sequence,
        max_attempts=settings.job_max_attempts,
        crm_enabled=settings.crm_provider != "none",
    )

    app.include_router(health.router)
    app.include_router(leads.router)
    app.include_router(tasks.router)
    app.include_router(admin.router)
    app.include_router(webhooks.router)
    app.include_router(inbound.router)
    app.include_router(metrics.router)
    app.include_router(dashboard.router)
    app.include_router(web_routes.router)
    app.add_exception_handler(dashboard.LoginRequired, dashboard.login_redirect)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app
