"""Request/response schemas for the public API."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.services.normalize import (
    MESSAGE_MAX,
    InvalidInputError,
    clean_message,
    clean_name,
    normalize_us_phone,
)


class LeadIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_max_length=2000)

    name: str = Field(max_length=200)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=25)
    message: str | None = Field(default=None, max_length=MESSAGE_MAX * 2)
    source: str = Field(default="api", pattern=r"^[a-z0-9_\-]{1,32}$")

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        try:
            return clean_name(v)
        except InvalidInputError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        try:
            return normalize_us_phone(v)
        except InvalidInputError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("email", mode="before")
    @classmethod
    def _email_blank(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.strip()
            return v.lower() or None
        return v

    @field_validator("message")
    @classmethod
    def _message(cls, v: str | None) -> str | None:
        if v is None:
            return None
        try:
            return clean_message(v) or None
        except InvalidInputError as exc:
            raise ValueError(str(exc)) from exc

    @model_validator(mode="after")
    def _contact_required(self) -> LeadIn:
        if not self.phone and not self.email:
            raise ValueError("at least one of phone or email is required")
        return self


class ScoreOut(BaseModel):
    score: int | None
    summary: str | None
    next_step: str | None
    confidence: float | None
    flags: list[str]
    scored_by: str | None
    latency_ms: int | None


class LeadOut(BaseModel):
    id: uuid.UUID
    name: str
    source: str
    stage: str
    created_at: datetime
    scoring: ScoreOut
