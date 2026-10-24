from __future__ import annotations

import json
from typing import Any

from app.ai.schemas import LeadContext

FAKE_KEY = "fake-key-for-tests"  # pragma: allowlist secret


def ctx(**overrides: Any) -> LeadContext:
    base: dict[str, Any] = {
        "lead_id": "lead-1",
        "name": "Jordan Alvarez",
        "source": "api",
        "message": "Need a quote to replace an old roof.",
        "has_phone": True,
        "has_email": True,
        "email_domain": "example.com",
    }
    base.update(overrides)
    return LeadContext(**base)


def model_json(**overrides: Any) -> str:
    body: dict[str, Any] = {
        "score": 82,
        "summary": "Homeowner wants a roof replacement quote. Reachable by phone.",
        "next_step": "call_now",
        "confidence": 0.9,
        "flags": [],
    }
    body.update(overrides)
    return json.dumps(body)


class FakeClock:
    def __init__(self, start: float = 100.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
