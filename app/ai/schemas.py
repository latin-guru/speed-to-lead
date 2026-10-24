"""Scoring input/output contracts. Model output must validate against ``ScoreResult``."""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

SUMMARY_MAX = 300
_SENTENCE_END_RE = re.compile(r"[.!?]+(?=\s|$)")

Flag = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_:\-]{1,48}$")]


class NextStep(enum.StrEnum):
    CALL_NOW = "call_now"
    SEND_INFO = "send_info"
    NURTURE = "nurture"
    DISQUALIFY = "disqualify"
    HUMAN_REVIEW = "human_review"


class ScoreResult(BaseModel):
    """The only shape a scorer may return. Extra keys are rejected."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    score: int = Field(ge=0, le=100)
    summary: str = Field(min_length=1, max_length=SUMMARY_MAX)
    next_step: NextStep
    confidence: float = Field(ge=0.0, le=1.0)
    flags: list[Flag] = Field(default_factory=list, max_length=10)

    @field_validator("summary")
    @classmethod
    def _two_sentences_max(cls, v: str) -> str:
        v = " ".join(v.split())
        if len(_SENTENCE_END_RE.findall(v)) > 2:
            raise ValueError("summary must be at most two sentences")
        return v

    @field_validator("next_step", mode="before")
    @classmethod
    def _coerce_enum(cls, v: object) -> object:
        # Strict mode would reject a plain string from JSON; accept exact enum values only.
        if isinstance(v, str):
            return NextStep(v)
        return v


@dataclass(frozen=True)
class LeadContext:
    """What a scorer sees. Raw phone/email are deliberately excluded (data minimization)."""

    lead_id: str
    name: str
    source: str
    message: str
    has_phone: bool
    has_email: bool
    email_domain: str | None = None
