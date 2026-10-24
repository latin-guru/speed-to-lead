from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import Settings
from app.main import create_app
from app.schemas import LeadIn
from app.services.leads import create_lead
from app.worker import Worker

FIXTURES = Path(__file__).parent / "fixtures"
META_SECRET = "unit-test-meta-secret"  # pragma: allowlist secret
META_VERIFY = "unit-test-verify-token"  # pragma: allowlist secret
ADMIN_TOKEN = "unit-test-admin-token"  # pragma: allowlist secret
INBOUND_SECRET = "unit-test-inbound-secret"  # pragma: allowlist secret
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "environment": "test",
        "database_url": "sqlite://",
        "secret_key": SecretStr("unit-test-secret-key"),  # pragma: allowlist secret
        "meta_app_secret": SecretStr(META_SECRET),
        "meta_verify_token": SecretStr(META_VERIFY),
        "admin_token": SecretStr(ADMIN_TOKEN),
        "inbound_webhook_secret": SecretStr(INBOUND_SECRET),
        # Tests run on the real clock; the quiet-hours tests turn this on with a fixed clock.
        "quiet_hours_enabled": False,
        "log_json": True,
        "ai_provider": "mock",
        "rate_limit_capacity": 50,
        "phone_daily_cap": 3,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


class FixedClock:
    """A clock tests move by hand."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 3, 2, 15, 0, tzinfo=UTC)  # a Monday, 15:00 UTC

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> datetime:
        self.now += timedelta(**kwargs)
        return self.now


def make_worker(app: Any, clock: Any = None, **kwargs: Any) -> Worker:
    """A worker sharing the app's database and job context (runs jobs in-process)."""
    opts: dict[str, Any] = {"worker_id": "test-worker", "rng": lambda: 0.5}
    if clock is not None:
        opts["clock"] = clock
    opts.update(kwargs)
    return Worker(app.state.session_factory, app.state.job_context, **opts)


def seed_lead(app: Any, clock: FixedClock | None = None, **overrides: Any) -> uuid.UUID:
    """Create a lead (and its jobs) the way capture does, at ``clock.now``."""
    data = LeadIn.model_validate(valid_lead(**overrides))
    now = clock() if clock is not None else None
    with app.state.session_factory() as session:
        lead = create_lead(session, data, actor="test", now=now)
        app.state.intake.on_lead_created(session, lead, now=now)
        session.commit()
        return lead.id


def drain(client: TestClient, clock: Any = None) -> int:
    """Run every due job now. Returns how many ran."""
    return make_worker(client.app, clock).run_until_idle()


class SleepRecorder:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def sleeper() -> SleepRecorder:
    return SleepRecorder()


@pytest.fixture
def client(settings: Settings, sleeper: SleepRecorder) -> Iterator[TestClient]:
    app = create_app(settings, sleep=sleeper)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def meta_payload() -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / "meta_leadgen.json").read_text())
    return copy.deepcopy(data)


def valid_lead(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "Jordan Alvarez",
        "email": "jordan.alvarez@example.com",
        "phone": "(202) 555-0123",
        "message": "Need a quote to replace an old asphalt roof before winter.",
    }
    body.update(overrides)
    return body
