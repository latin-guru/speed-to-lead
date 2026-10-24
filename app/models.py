"""ORM models: Lead, Activity (timeline) and WebhookEvent (idempotency ledger)."""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class LeadStage(enum.StrEnum):
    NEW = "new"
    CONTACTED = "contacted"
    QUALIFIED = "qualified"
    WON = "won"
    LOST = "lost"


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(80))
    email: Mapped[str | None] = mapped_column(String(254), index=True)
    phone: Mapped[str | None] = mapped_column(String(16), index=True)  # E.164
    source: Mapped[str] = mapped_column(String(32), index=True)
    message: Mapped[str | None] = mapped_column(Text)
    # Stage is only changed by explicit business actions, never by the AI scorer.
    stage: Mapped[str] = mapped_column(String(16), default=LeadStage.NEW.value, index=True)

    # Scoring (all nullable until scored). next_step is a *suggestion* only.
    score: Mapped[int | None] = mapped_column(Integer)
    score_summary: Mapped[str | None] = mapped_column(String(300))
    next_step: Mapped[str | None] = mapped_column(String(16))
    score_confidence: Mapped[float | None] = mapped_column(Float)
    score_flags: Mapped[list[str] | None] = mapped_column(JSON)
    scored_by: Mapped[str | None] = mapped_column(String(16))  # mock | claude | grok | fallback
    score_provider: Mapped[str | None] = mapped_column(String(16))  # provider attempted
    score_latency_ms: Mapped[int | None] = mapped_column(Integer)
    scored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    first_response_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    activities: Mapped[list[Activity]] = relationship(
        back_populates="lead", order_by="Activity.created_at", cascade="all, delete-orphan"
    )


class Activity(Base):
    __tablename__ = "activities"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    lead_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("leads.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32), index=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    actor: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    lead: Mapped[Lead] = relationship(back_populates="activities")


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    __table_args__ = (UniqueConstraint("provider", "event_id", name="uq_webhook_provider_event"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(16))
    event_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))  # processed | rejected | needs_fetch
    lead_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("leads.id", ondelete="SET NULL"))
    reason: Mapped[str | None] = mapped_column(String(200))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
