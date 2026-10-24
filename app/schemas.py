"""Request/response schemas for the public API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.models import LeadStage
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
    # IANA timezone for quiet hours, e.g. "America/Denver". None -> the business timezone.
    timezone: str | None = Field(default=None, max_length=64)

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        v = v.strip()
        if "/" not in v and v != "UTC":
            raise ValueError("timezone must be an IANA name like America/Denver")
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("unknown timezone") from exc
        return v

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


class ActivityOut(BaseModel):
    kind: str
    actor: str
    created_at: datetime
    detail: dict[str, Any]


class LeadDetailOut(LeadOut):
    lost_reason: str | None
    timezone: str | None
    sequence_state: str
    opted_out: bool
    first_response_at: datetime | None
    first_response_seconds: float | None
    crm_contact_id: str | None
    crm_deal_id: str | None
    timeline: list[ActivityOut]


# Who is acting, e.g. "dana". Stored as "human:<name>".
ActorName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_.\-]{1,24}$")]


class StageChangeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: LeadStage
    actor: ActorName
    reason: str | None = Field(default=None, max_length=200)


class ContactLogIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: ActorName
    channel: Literal["call", "sms", "email", "in_person"]
    note: str | None = Field(default=None, max_length=300)


class TaskOut(BaseModel):
    id: int
    lead_id: uuid.UUID
    reason: str
    detail: str | None
    assignee: str | None
    status: str
    created_at: datetime
    done_at: datetime | None


class TaskUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["open", "done"] | None = None
    assignee: str | None = Field(default=None, pattern=r"^[a-z0-9_.\-]{1,24}$")


class JobOut(BaseModel):
    id: int
    kind: str
    status: str
    attempts: int
    max_attempts: int
    run_at: datetime
    idempotency_key: str
    last_error: str | None
    lead_id: uuid.UUID | None
    updated_at: datetime


class KillSwitchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paused: bool
    actor: ActorName


class KillSwitchOut(BaseModel):
    paused: bool
    env_paused: bool
    db_paused: bool
    released_jobs: int = 0


class InboundReplyIn(BaseModel):
    """Body of the (mock) inbound SMS webhook."""

    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(pattern=r"^[A-Za-z0-9_\-]{1,64}$")
    from_phone: str = Field(alias="from", max_length=25)
    body: str = Field(max_length=1600)
