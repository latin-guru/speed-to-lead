"""Apply an AI score to a lead (runs inside the ``score_lead`` job)."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from app.ai.schemas import LeadContext, NextStep
from app.ai.service import ScoringService
from app.models import Lead
from app.services.handoff import AI_HUMAN_REVIEW, LOW_CONFIDENCE, handoff
from app.services.timeline import add_activity

log = logging.getLogger(__name__)


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


def score_lead(
    session: Session,
    lead_id: uuid.UUID,
    service: ScoringService,
    *,
    now: datetime,
) -> str:
    """Score once. Writes scoring fields only; **never** touches ``stage``.

    A ``human_review`` suggestion or low confidence hands the lead to a person. A scorer
    crash propagates, so the queue retries it with backoff and dead-letters it at the end.
    """
    lead = session.get(Lead, lead_id)
    if lead is None:
        return "skipped:lead_missing"
    if lead.scored_at is not None:
        return "already_scored"

    outcome = service.score(lead_context(lead))
    r = outcome.result
    lead.score = r.score
    lead.score_summary = r.summary
    lead.next_step = r.next_step.value
    lead.score_confidence = r.confidence
    lead.score_flags = list(r.flags)
    lead.scored_by = outcome.scored_by
    lead.score_provider = outcome.provider
    lead.score_latency_ms = outcome.latency_ms
    lead.scored_at = now
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
    if r.next_step is NextStep.HUMAN_REVIEW:
        handoff(session, lead, AI_HUMAN_REVIEW, now=now, detail=r.summary)
    elif r.confidence < service.min_confidence:
        handoff(session, lead, LOW_CONFIDENCE, now=now, detail=r.summary)
    return "scored"
