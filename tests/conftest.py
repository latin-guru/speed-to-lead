from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import Settings
from app.main import create_app

FIXTURES = Path(__file__).parent / "fixtures"
META_SECRET = "unit-test-meta-secret"  # pragma: allowlist secret
META_VERIFY = "unit-test-verify-token"  # pragma: allowlist secret


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "environment": "test",
        "database_url": "sqlite://",
        "secret_key": SecretStr("unit-test-secret-key"),  # pragma: allowlist secret
        "meta_app_secret": SecretStr(META_SECRET),
        "meta_verify_token": SecretStr(META_VERIFY),
        "log_json": True,
        "ai_provider": "mock",
        "rate_limit_capacity": 50,
        "phone_daily_cap": 3,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


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
