"""Lead creation, the activity timeline, and the scoring dispatch seam."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, time, timedelta
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai.schemas import LeadContext
from app.ai.service import ScoringService
from app.models import Activity, Lead, utcnow
from app.schemas import LeadIn, LeadOut, ScoreOut

log = logging.getLogger(__name__)


def add_activity(
    session: Session, lead: Lead, kind: str, actor: str, detail: dict[str, Any] | None = None
) -> Activity:
    activity = Activity(lead_id=lead.id, kind=kind, actor=actor, detail=detail or {})
    session.add(activity)
    return activity


def create_lead(
    session: Session, data: LeadIn, *, actor: str, detail: dict[str, Any] | None = None
) -> Lead:
    """Insert a lead and its ``lead_created`` activity. The caller commits."""
    lead = Lead(
        name=data.name,
        email=data.email,
        phone=data.phone,
        source=data.source,
        message=data.message,
    )
    session.add(lead)
    session.flush()  # assigns defaults (id, created_at)
    add_activity(session, lead, "lead_created", actor, {"source": data.source, **(detail or {})})
    log.info("lead_created", extra={"lead_id": str(lead.id), "source": lead.source})
    return lead


def utc_day_bounds(now: datetime) -> tuple[datetime, datetime]:
    start = datetime.combine(now.astimezone(UTC).date(), time.min, tzinfo=UTC)
    return start, start + timedelta(days=1)


def leads_today_for_phone(session: Session, phone: str, *, now: datetime | None = None) -> int:
    start, end = utc_day_bounds(now or utcnow())
    stmt = select(func.count(Lead.id)).where(
        Lead.phone == phone, Lead.created_at >= start, Lead.created_at < end
    )
    return int(session.scalar(stmt) or 0)


def seconds_until_utc_midnight(now: datetime | None = None) -> int:
    current = now or utcnow()
    _, end = utc_day_bounds(current)
    return max(1, int((end - current).total_seconds()))


def lead_context(lead: Lead) -> LeadContext:
    domain = lead.email.rsplit("@", 1)[1] if lead.email and "@" in lead.email else None
    return LeadContext(
        lead_id=str(lead.id),
        name=lead.name,
        source=lead.source,
        message=lead.message or "",
        has_phone=bool(lead.phone),
        has_email=bool(lead.email),
        email_domain=domain,
    )


class ScoringDispatcher(Protocol):
    """Seam for session 3: swap the inline dispatcher for a queue-backed one."""

    def dispatch(self, session: Session, lead: Lead) -> None: ...


class InlineScoringDispatcher:
    """Scores synchronously right after capture (bounded by the AI time budget)."""

    def __init__(self, service: ScoringService) -> None:
        self.service = service

    def dispatch(self, session: Session, lead: Lead) -> None:
        try:
            outcome = self.service.score(lead_context(lead))
        except Exception:  # never lose a captured lead because scoring broke
            log.exception("scoring_dispatch_failed", extra={"lead_id": str(lead.id)})
            add_activity(session, lead, "ai_scoring_failed", "system")
            session.commit()
            return
        r = outcome.result
        # Only scoring fields are written. Stage is never touched: next_step is a suggestion.
        lead.score = r.score
        lead.score_summary = r.summary
        lead.next_step = r.next_step.value
        lead.score_confidence = r.confidence
        lead.score_flags = list(r.flags)
        lead.scored_by = outcome.scored_by
        lead.score_provider = outcome.provider
        lead.score_latency_ms = outcome.latency_ms
        lead.scored_at = utcnow()
        add_activity(
            session,
            lead,
            "ai_scored",
            f"ai:{outcome.scored_by}",
            {
                "score": r.score,
                "next_step": r.next_step.value,
                "confidence": r.confidence,
                "flags": list(r.flags),
                "scored_by": outcome.scored_by,
                "provider": outcome.provider,
                "latency_ms": outcome.latency_ms,
                "attempts": outcome.attempts,
                "error": outcome.error,
            },
        )
        session.commit()
        log.info(
            "ai_scored",
            extra={
                "lead_id": str(lead.id),
                "scored_by": outcome.scored_by,
                "score": r.score,
                "next_step": r.next_step.value,
                "latency_ms": outcome.latency_ms,
            },
        )


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
