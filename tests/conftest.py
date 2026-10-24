from __future__ import annotations

import base64
import copy
import hashlib
import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import Settings
from app.main import create_app
from app.models import User, UserRole
from app.schemas import LeadIn
from app.security import passwords
from app.services.leads import create_lead
from app.worker import Worker

# argon2 with its production parameters costs ~50 ms per hash; tests use a cheap profile.
passwords.configure(PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1))

FIXTURES = Path(__file__).parent / "fixtures"
META_SECRET = "unit-test-meta-secret"  # pragma: allowlist secret
META_VERIFY = "unit-test-verify-token"  # pragma: allowlist secret
ADMIN_TOKEN = "unit-test-admin-token"  # pragma: allowlist secret
INBOUND_SECRET = "unit-test-inbound-secret"  # pragma: allowlist secret
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
# A fixed, test-only Fernet key (derived, so no key material is written in the repo).
FIELD_KEY = base64.urlsafe_b64encode(hashlib.sha256(b"unit-test-field-key").digest()).decode()
BLIND_INDEX = "unit-test-blind-index-key"  # pragma: allowlist secret


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "environment": "test",
        "database_url": "sqlite://",
        "secret_key": SecretStr("unit-test-secret-key"),  # pragma: allowlist secret
        "meta_app_secret": SecretStr(META_SECRET),
        "meta_verify_token": SecretStr(META_VERIFY),
        "admin_token": SecretStr(ADMIN_TOKEN),
        "inbound_webhook_secret": SecretStr(INBOUND_SECRET),
        "field_encryption_key": SecretStr(FIELD_KEY),
        "blind_index_key": SecretStr(BLIND_INDEX),
        # Tests run on the real clock; the quiet-hours tests turn this on with a fixed clock.
        "quiet_hours_enabled": False,
        "log_json": True,
        "ai_provider": "mock",
        "rate_limit_capacity": 50,
        "phone_daily_cap": 3,
        # Most tests log in as an admin without two-factor; tests/integration/test_mfa.py
        # covers the required-for-admins policy.
        "admin_2fa_required": False,
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


@pytest.fixture(autouse=True)
def _restore_field_crypto() -> Iterator[None]:
    """Commands and apps install process-wide field crypto; put the previous one back."""
    from app.security import fields

    saved = fields._active
    yield
    fields._active = saved


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


PASSWORD = "correct horse battery"  # pragma: allowlist secret
ROLE_EMAILS = {role: f"{role.value}@example.com" for role in UserRole}


def add_user(app: Any, role: UserRole | str, email: str | None = None, **kw: Any) -> int:
    """Create a dashboard user directly in the app's database. Returns the id."""
    role = UserRole(role)
    with app.state.session_factory() as session:
        user = User(
            email=email or ROLE_EMAILS[role],
            password_hash=passwords.hash_password(kw.pop("password", PASSWORD)),
            role=role.value,
            active=kw.pop("active", True),
            failed_logins=0,
            **kw,
        )
        session.add(user)
        session.commit()
        return user.id
