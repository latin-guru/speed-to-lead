"""Sequence definition (YAML), validation and template rendering."""

from __future__ import annotations

import enum
import string
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DEFAULT_SEQUENCE_PATH = Path(__file__).parent / "default_sequence.yaml"
PLACEHOLDERS = frozenset(
    {"first_name", "company", "callback_minutes", "received_when", "call_when"}
)
SMS_MAX = 320  # two SMS segments


class Channel(enum.StrEnum):
    SMS = "sms"
    EMAIL = "email"


def _placeholders(template: str) -> set[str]:
    names: set[str] = set()
    for _, field, spec, conv in string.Formatter().parse(template):
        if field is None:
            continue
        if not field.isidentifier() or spec or conv:
            raise ValueError(f"unsupported placeholder: {{{field}}}")
        names.add(field)
    return names


class SequenceStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9_]{1,32}$")
    channel: Channel
    delay_minutes: int = Field(ge=0, le=60 * 24 * 30)
    transactional: bool = False
    subject: str | None = Field(default=None, max_length=200)
    body: str = Field(min_length=1, max_length=2000)
    # Used instead of body/subject when the lead arrived outside the sending window, so the
    # first message does not promise a callback "within 15 minutes" at 2 a.m.
    after_hours_body: str | None = Field(default=None, min_length=1, max_length=2000)
    after_hours_subject: str | None = Field(default=None, max_length=200)

    @field_validator("body", "subject", "after_hours_body", "after_hours_subject")
    @classmethod
    def _known_placeholders(cls, v: str | None) -> str | None:
        if v is not None:
            unknown = _placeholders(v) - PLACEHOLDERS
            if unknown:
                raise ValueError(f"unknown placeholders: {sorted(unknown)}")
        return v

    @model_validator(mode="after")
    def _channel_rules(self) -> SequenceStep:
        if self.channel is Channel.EMAIL and not self.subject:
            raise ValueError(f"email step {self.id!r} needs a subject")
        if self.channel is Channel.SMS and (self.subject or self.after_hours_subject):
            raise ValueError(f"sms step {self.id!r} cannot have a subject")
        if self.after_hours_subject and not self.after_hours_body:
            raise ValueError(f"step {self.id!r}: after_hours_subject needs after_hours_body")
        return self


class Sequence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z0-9_]{1,48}$")
    steps: tuple[SequenceStep, ...] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def _unique_ids(self) -> Sequence:
        ids = [s.id for s in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("step ids must be unique")
        return self

    def step(self, step_id: str) -> SequenceStep | None:
        return next((s for s in self.steps if s.id == step_id), None)


def load_sequence(path: str | Path | None = None) -> Sequence:
    raw: Any = yaml.safe_load(Path(path or DEFAULT_SEQUENCE_PATH).read_text(encoding="utf-8"))
    return Sequence.model_validate(raw)


def first_name(name: str) -> str:
    return name.split()[0] if name.split() else "there"


def render(
    template: str,
    *,
    name: str,
    company: str,
    callback_minutes: int,
    received_when: str = "recently",
    call_when: str = "soon",
) -> str:
    """Fill a template. Lead data is only ever a *value*, never parsed as a template."""
    values = {
        "first_name": first_name(name),
        "company": company,
        "callback_minutes": str(callback_minutes),
        "received_when": received_when,
        "call_when": call_when,
    }
    return template.format_map(values)
