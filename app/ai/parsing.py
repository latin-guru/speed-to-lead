"""Parse provider text into a ``ScoreResult``: strict first, then exactly one repair attempt."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ValidationError

from app.ai.base import InvalidOutputError
from app.ai.schemas import ScoreResult

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


def _validate(obj: Any) -> ScoreResult:
    if not isinstance(obj, dict):
        raise InvalidOutputError("model output is not a JSON object")
    try:
        return ScoreResult.model_validate(obj)
    except ValidationError as exc:
        raise InvalidOutputError(
            f"model output failed schema: {exc.error_count()} error(s)"
        ) from exc


def _repair(text: str) -> str:
    text = _FENCE_RE.sub("", text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise InvalidOutputError("no JSON object found in model output")
    return _TRAILING_COMMA_RE.sub(r"\1", text[start : end + 1])


def parse_score_output(text: str) -> ScoreResult:
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        try:
            obj = json.loads(_repair(text))
        except json.JSONDecodeError as exc:
            raise InvalidOutputError("model output is not valid JSON after repair") from exc
    return _validate(obj)
