"""ORM models.

- ``Lead``, ``Activity`` (timeline), ``WebhookEvent`` (idempotency ledger)  - session 1
- ``Job`` (DB-backed queue), ``OutboxMessage`` (mock sends), ``Task`` (human handoff),
  ``SystemFlag`` (kill switch)                                                - session 3
- ``User``, ``UserSession`` (server-side login sessions), ``AuditEvent``,
  ``RecoveryCode`` (two-factor)                                               - session 5

The schema is owned by Alembic; keep ``migrations/versions`` in step with this file
(``tests/integration/test_migrations.py`` fails if they drift).
"""

from __future__ import annotations

import enum
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    ColumnElement,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, UTCDateTime
from app.security import fields


def utcnow() -> datetime:
    return datetime.now(UTC)


class LeadStage(enum.StrEnum):
    NEW = "new"
    QUALIFIED = "qualified"
    CONTACTED = "contacted"
    WON = "won"
    LOST = "lost"


class SequenceState(enum.StrEnum):
    ACTIVE = "active"  # automated follow-ups may send
    PAUSED = "paused"  # handed to a human; pending sends cancelled
    STOPPED = "stopped"  # opted out, closed, or contacted by a human


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(80))
    # Contact details are encrypted at rest (app/security/fields.py). ``*_bidx`` is a keyed
    # HMAC of the normalized value, for equality lookups without decrypting. Read and write
    # them through the ``phone`` / ``email`` properties below.
    email_enc: Mapped[str | None] = mapped_column(Text)
    email_bidx: Mapped[str | None] = mapped_column(String(64), index=True)
    phone_enc: Mapped[str | None] = mapped_column(Text)
    phone_bidx: Mapped[str | None] = mapped_column(String(64), index=True)  # of E.164
    source: Mapped[str] = mapped_column(String(32), index=True)
    message: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str | None] = mapped_column(String(64))  # IANA name; None = business tz
    # Stage is only changed by explicit business actions, never by the AI scorer.
    stage: Mapped[str] = mapped_column(String(16), default=LeadStage.NEW.value, index=True)
    lost_reason: Mapped[str | None] = mapped_column(String(200))

    # Scoring (all nullable until scored). next_step is a *suggestion* only.
    score: Mapped[int | None] = mapped_column(Integer)
    score_summary: Mapped[str | None] = mapped_column(String(300))
    next_step: Mapped[str | None] = mapped_column(String(16))
    score_confidence: Mapped[float | None] = mapped_column(Float)
    score_flags: Mapped[list[str] | None] = mapped_column(JSON)
    scored_by: Mapped[str | None] = mapped_column(String(16))  # mock | claude | grok | fallback
    score_provider: Mapped[str | None] = mapped_column(String(16))  # provider attempted
    score_latency_ms: Mapped[int | None] = mapped_column(Integer)
    scored_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    # Follow-up sequence
    sequence_state: Mapped[str] = mapped_column(String(16), default=SequenceState.ACTIVE.value)
    opted_out_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    # Who works this lead (a rep or admin). Reps can only reveal contact details of their
    # own leads; an unassigned lead must be assigned to them first.
    assignee_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    # Set by the retention job when the lead's personal data was removed.
    anonymized_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    # CRM sync bookkeeping (ids in the external CRM, set by the sync job)
    crm_contact_id: Mapped[str | None] = mapped_column(String(64))
    crm_deal_id: Mapped[str | None] = mapped_column(String(64))
    crm_synced_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    first_response_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    activities: Mapped[list[Activity]] = relationship(
        back_populates="lead", order_by="Activity.id", cascade="all, delete-orphan"
    )
    assignee: Mapped[User | None] = relationship(lazy="joined")

    @property
    def phone(self) -> str | None:
        """E.164 phone, decrypted on read."""
        return fields.decrypt_optional(self.phone_enc)

    @phone.setter
    def phone(self, value: str | None) -> None:
        self.phone_enc = fields.encrypt_optional(value)
        self.phone_bidx = fields.index_optional("phone", value)

    @property
    def email(self) -> str | None:
        return fields.decrypt_optional(self.email_enc)

    @email.setter
    def email(self, value: str | None) -> None:
        self.email_enc = fields.encrypt_optional(value)
        self.email_bidx = fields.index_optional("email", value)

    @staticmethod
    def phone_is(phone: str) -> ColumnElement[bool]:
        """SQL condition "this lead has ``phone``" (via the blind index)."""
        return Lead.phone_bidx == fields.active().blind_index("phone", phone)

    @staticmethod
    def email_is(email: str) -> ColumnElement[bool]:
        return Lead.email_bidx == fields.active().blind_index("email", email)


class Activity(Base):
    __tablename__ = "activities"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    lead_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("leads.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32), index=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    actor: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    lead: Mapped[Lead] = relationship(back_populates="activities")


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    __table_args__ = (UniqueConstraint("provider", "event_id", name="uq_webhook_provider_event"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(16))
    event_id: Mapped[str] = mapped_column(String(64))
    # processed | rejected | needs_fetch | unmatched
    status: Mapped[str] = mapped_column(String(16))
    lead_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("leads.id", ondelete="SET NULL"))
    reason: Mapped[str | None] = mapped_column(String(200))
    received_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class JobStatus(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    DEAD = "dead"  # dead-letter: out of attempts or a permanent error
    CANCELLED = "cancelled"  # e.g. a follow-up send cancelled by STOP or a handoff


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_claim", "status", "run_at"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    run_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    status: Mapped[str] = mapped_column(String(16), default=JobStatus.QUEUED.value)
    locked_by: Mapped[str | None] = mapped_column(String(64))
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    last_error: Mapped[str | None] = mapped_column(String(500))
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("leads.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class OutboxMessage(Base):
    """A message a MOCK sender "sent". Nothing here ever leaves the process.

    ``idempotency_key`` (``<lead_id>:<step>``) is UNIQUE, which is what makes a send
    at-most-once even if the job that sends it runs twice.
    """

    __tablename__ = "outbox_messages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    lead_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("leads.id", ondelete="CASCADE"), index=True
    )
    step: Mapped[str] = mapped_column(String(32))
    channel: Mapped[str] = mapped_column(String(8))  # sms | email
    to_address_enc: Mapped[str] = mapped_column(Text)  # encrypted, like the lead's contacts
    subject: Mapped[str | None] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    sender: Mapped[str] = mapped_column(String(32))  # e.g. "MOCK-sms"
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    transactional: Mapped[bool] = mapped_column(default=False)
    sent_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)

    @property
    def to_address(self) -> str:
        return fields.active().decrypt(self.to_address_enc)

    @to_address.setter
    def to_address(self, value: str) -> None:
        self.to_address_enc = fields.active().encrypt(value)


class TaskStatus(enum.StrEnum):
    OPEN = "open"
    DONE = "done"


class Task(Base):
    """Work for a human: created when automation hands a lead off."""

    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    lead_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("leads.id", ondelete="CASCADE"), index=True
    )
    reason: Mapped[str] = mapped_column(String(32))  # lead_replied | ai_human_review | ...
    detail: Mapped[str | None] = mapped_column(String(300))
    assignee: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(8), default=TaskStatus.OPEN.value, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    done_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class SystemFlag(Base):
    """Small key/value switches an operator can flip at runtime (e.g. ``sends_paused``)."""

    __tablename__ = "system_flags"

    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    value: Mapped[str] = mapped_column(String(64))
    updated_by: Mapped[str] = mapped_column(String(48))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class UserRole(enum.StrEnum):
    ADMIN = "admin"  # everything, including users, retries, the kill switch and the audit log
    REP = "rep"  # works leads: stages, tasks, and contact details of leads assigned to them
    VIEWER = "viewer"  # read-only, contact details always masked


class User(Base):
    """A dashboard account. Passwords are argon2id hashes (app/security/passwords.py)."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)  # stored lowercased
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16))
    active: Mapped[bool] = mapped_column(default=True)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    # Two-factor (TOTP). Secrets are encrypted with the field-encryption key. ``pending`` is
    # a secret being enrolled (not active until a code from it is confirmed).
    totp_secret_enc: Mapped[str | None] = mapped_column(Text)
    totp_pending_enc: Mapped[str | None] = mapped_column(Text)
    totp_enabled_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    totp_last_step: Mapped[int | None] = mapped_column(Integer)  # replay protection

    @property
    def mfa_enabled(self) -> bool:
        return self.totp_enabled_at is not None and self.totp_secret_enc is not None

    @property
    def actor_name(self) -> str:
        """Short name for the lead timeline (``human:<name>``), from the email's local part."""
        local = re.sub(r"[^a-z0-9_.\-]", "", self.email.split("@", 1)[0].lower())[:24]
        return local or f"user{self.id}"


class RecoveryCode(Base):
    """A single-use two-factor recovery code. Only a SHA-256 of the code is stored."""

    __tablename__ = "recovery_codes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class UserSession(Base):
    """A server-side login session. The cookie holds a random id; only its hash is stored,
    so a database leak does not hand out live sessions. Logout or an admin change revokes it.
    """

    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())  # absolute limit
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    ip_hash: Mapped[str | None] = mapped_column(String(32))
    user_agent: Mapped[str | None] = mapped_column(String(200))

    user: Mapped[User] = relationship(lazy="joined")


class AuditEvent(Base):
    """Who did what, when. Summaries never hold contact details; IPs are stored as a keyed
    hash, so events from one address can be grouped without keeping the address."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(254))  # user email, "api-token", "system", "cli"
    role: Mapped[str | None] = mapped_column(String(16))
    action: Mapped[str] = mapped_column(String(48), index=True)
    outcome: Mapped[str] = mapped_column(String(16), default="ok")  # ok | denied | failed
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(64), index=True)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    ip_hash: Mapped[str | None] = mapped_column(String(32))
    user_agent: Mapped[str | None] = mapped_column(String(200))
