"""Lead creation, intake (what happens right after capture) and API views of a lead."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.crm.sync import enqueue_crm_sync
from app.followups.engine import enqueue_sequence
from app.followups.sequence import Sequence
from app.jobs import kinds
from app.jobs.queue import enqueue
from app.models import Lead, utcnow
from app.schemas import ActivityOut, LeadDetailOut, LeadIn, LeadOut, ScoreOut
from app.services.timeline import add_activity

log = logging.getLogger(__name__)

__all__ = ["Intake", "add_activity", "create_lead", "to_lead_detail", "to_lead_out"]


def create_lead(
    session: Session,
    data: LeadIn,
    *,
    actor: str,
    detail: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> Lead:
    """Insert a lead and its ``lead_created`` activity. The caller commits."""
    lead = Lead(
        name=data.name,
        email=data.email,
        phone=data.phone,
        source=data.source,
        message=data.message,
        timezone=data.timezone,
        created_at=now or utcnow(),
    )
    session.add(lead)
    session.flush()  # assigns defaults (id)
    add_activity(session, lead, "lead_created", actor, {"source": data.source, **(detail or {})})
    log.info("lead_created", extra={"lead_id": str(lead.id), "source": lead.source})
    return lead


@dataclass(frozen=True)
class Intake:
    """Queues the work for a new lead: scoring, the follow-up sequence and a CRM sync.

    Called *before* the lead's transaction commits, so the lead and its jobs are saved
    together or not at all (a transactional outbox). Capture returns without waiting for
    any of it; the worker picks the jobs up immediately.
    """

    sequence: Sequence
    max_attempts: int = 5
    crm_enabled: bool = True

    def on_lead_created(self, session: Session, lead: Lead, *, now: datetime | None = None) -> None:
        now = now or utcnow()
        enqueue(
            session,
            kinds.SCORE_LEAD,
            {"lead_id": str(lead.id)},
            idempotency_key=kinds.score_key(lead.id),
            run_at=lead.created_at,  # same instant as the first sends; lower id runs first
            max_attempts=self.max_attempts,
            lead_id=lead.id,
            now=now,
        )
        enqueue_sequence(session, lead, self.sequence, max_attempts=self.max_attempts, now=now)
        enqueue_crm_sync(
            session, lead, max_attempts=self.max_attempts, now=now, enabled=self.crm_enabled
        )


def utc_day_bounds(now: datetime) -> tuple[datetime, datetime]:
    start = datetime.combine(now.astimezone(UTC).date(), time.min, tzinfo=UTC)
    return start, start + timedelta(days=1)


def leads_today_for_phone(session: Session, phone: str, *, now: datetime | None = None) -> int:
    start, end = utc_day_bounds(now or utcnow())
    stmt = select(func.count(Lead.id)).where(
        Lead.phone_is(phone), Lead.created_at >= start, Lead.created_at < end
    )
    return int(session.scalar(stmt) or 0)


def seconds_until_utc_midnight(now: datetime | None = None) -> int:
    current = now or utcnow()
    _, end = utc_day_bounds(current)
    return max(1, int((end - current).total_seconds()))


def to_lead_out(lead: Lead) -> LeadOut:
    return LeadOut(
        id=lead.id,
        name=lead.name,
        source=lead.source,
        stage=lead.stage,
        created_at=lead.created_at,
        scoring=ScoreOut(
            score=lead.score,
            summary=lead.score_summary,
            next_step=lead.next_step,
            confidence=lead.score_confidence,
            flags=lead.score_flags or [],
            scored_by=lead.scored_by,
            latency_ms=lead.score_latency_ms,
        ),
    )


def to_lead_detail(lead: Lead) -> LeadDetailOut:
    seconds = None
    if lead.first_response_at is not None:
        seconds = round((lead.first_response_at - lead.created_at).total_seconds(), 3)
    base = to_lead_out(lead)
    return LeadDetailOut(
        **base.model_dump(),
        lost_reason=lead.lost_reason,
        timezone=lead.timezone,
        sequence_state=lead.sequence_state,
        opted_out=lead.opted_out_at is not None,
        first_response_at=lead.first_response_at,
        first_response_seconds=seconds,
        crm_contact_id=lead.crm_contact_id,
        crm_deal_id=lead.crm_deal_id,
        timeline=[
            ActivityOut(kind=a.kind, actor=a.actor, created_at=a.created_at, detail=a.detail)
            for a in lead.activities
        ],
    )
