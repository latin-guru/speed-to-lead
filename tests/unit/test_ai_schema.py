from typing import Any

import pytest
from pydantic import ValidationError

from app.ai.schemas import NextStep, ScoreResult

GOOD: dict[str, Any] = {
    "score": 70,
    "summary": "Solid lead. Wants an estimate soon.",
    "next_step": "send_info",
    "confidence": 0.8,
    "flags": ["urgent_need"],
}


def test_valid() -> None:
    r = ScoreResult.model_validate(GOOD)
    assert r.next_step is NextStep.SEND_INFO
    assert ScoreResult.model_validate({**GOOD, "confidence": 1}).confidence == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"score": 101},
        {"score": -1},
        {"score": "80"},  # strict: no string coercion
        {"score": 80.5},
        {"confidence": 1.5},
        {"confidence": "0.9"},
        {"next_step": "send_contract"},
        {"next_step": "CALL_NOW"},
        {"summary": ""},
        {"summary": "x" * 301},
        {"summary": "One. Two. Three."},
        {"flags": ["Has Spaces"]},
        {"flags": ["ok"] * 11},
        {"extra": "key"},
    ],
)
def test_invalid(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ScoreResult.model_validate({**GOOD, **overrides})


def test_missing_fields() -> None:
    with pytest.raises(ValidationError):
        ScoreResult.model_validate({"score": 50})
