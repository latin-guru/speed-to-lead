"""Data retention: remove personal data from leads older than ``RETENTION_DAYS``.

Run it on a schedule (daily is plenty), e.g. from cron or a scheduled job on the host::

    python -m app.cli retention            # or --dry-run to only count

Two modes (``RETENTION_MODE``):

- ``anonymize`` (default): the lead row stays, so funnel and response-time metrics keep
  their history, but the name becomes a placeholder; phone, email, message, timezone and
  the AI summary are cleared (with their blind indexes, so the lead no longer matches a
  phone number); the MOCK outbox copies are deleted; task notes are cleared; and timeline
  details keep only structured, non-free-text fields.
- ``delete``: the lead and everything attached to it are deleted.

It also deletes login sessions that ended more than a day ago. Audit events are kept
(they hold no contact details). Each run writes one ``retention_run`` audit event.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import Session

from app.models import Activity, Job, Lead, OutboxMessage, Task, UserSession, WebhookEvent
from app.services import audit
from app.services.timeline import add_activity

ANONYMIZED_NAME = "Removed (retention)"
# Timeline detail keys that never hold free text or contact details.
SAFE_DETAIL_KEYS = frozenset(
    {
        "source",
        "channel",
        "step",
        "sender",
        "variant",
        "from",
        "to",
        "score",
        "scored_by",
        "next_step",
        "confidence",
        "stage",
        "adapter",
        "cancelled_sends",
        "requeued_sends",
        "resumed_sends",
        "previous",
        "fields",
        "task_id",
        "backfill",
        "days",
    }
)
SESSION_GRACE = timedelta(days=1)


@dataclass(frozen=True)
class RetentionReport:
    cutoff: datetime
    mode: str
    leads: int
    outbox_removed: int
    sessions_removed: int
    dry_run: bool


def _scrub(detail: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in (detail or {}).items() if k in SAFE_DETAIL_KEYS}


def _anonymize(session: Session, lead: Lead, now: datetime, days: int) -> None:
    lead.name = ANONYMIZED_NAME
    lead.phone = None
    lead.email = None
    lead.message = None
    lead.timezone = None
    lead.score_summary = None
    lead.lost_reason = None
    lead.anonymized_at = now
    for activity in lead.activities:
        activity.detail = _scrub(activity.detail)
    session.execute(update(Task).where(Task.lead_id == lead.id).values(detail=None))
    add_activity(session, lead, "lead_anonymized", "system:retention", {"days": days})


def run_retention(
    session: Session,
    *,
    now: datetime,
    days: int,
    mode: Literal["anonymize", "delete"] = "anonymize",
    dry_run: bool = False,
) -> RetentionReport:
    """Apply the retention policy. The caller commits (nothing changes on a dry run)."""
    cutoff = now - timedelta(days=days)
    leads = list(
        session.scalars(select(Lead).where(Lead.created_at < cutoff, Lead.anonymized_at.is_(None)))
    )
    ids = [lead.id for lead in leads]
    outbox = (
        list(session.scalars(select(OutboxMessage.id).where(OutboxMessage.lead_id.in_(ids))))
        if ids
        else []
    )
    stale_sessions = list(
        session.scalars(
            select(UserSession.id).where(
                or_(
                    UserSession.expires_at < now - SESSION_GRACE,
                    UserSession.revoked_at < now - SESSION_GRACE,
                )
            )
        )
    )
    report = RetentionReport(
        cutoff=cutoff,
        mode=mode,
        leads=len(ids),
        outbox_removed=len(outbox),
        sessions_removed=len(stale_sessions),
        dry_run=dry_run,
    )
    if dry_run:
        return report
    if ids:
        session.execute(delete(OutboxMessage).where(OutboxMessage.lead_id.in_(ids)))
        if mode == "delete":
            # Explicit, so it does not depend on the database enforcing ON DELETE CASCADE.
            session.execute(delete(Task).where(Task.lead_id.in_(ids)))
            session.execute(delete(Job).where(Job.lead_id.in_(ids)))
            session.execute(delete(Activity).where(Activity.lead_id.in_(ids)))
            session.execute(
                update(WebhookEvent).where(WebhookEvent.lead_id.in_(ids)).values(lead_id=None)
            )
            session.execute(delete(Lead).where(Lead.id.in_(ids)))
            session.expire_all()
        else:
            for lead in leads:
                _anonymize(session, lead, now, days)
    if stale_sessions:
        session.execute(delete(UserSession).where(UserSession.id.in_(stale_sessions)))
    audit.record(
        session,
        actor="system",
        role=None,
        action="retention_run",
        summary={
            "mode": mode,
            "days": days,
            "leads": report.leads,
            "outbox_removed": report.outbox_removed,
            "sessions_removed": report.sessions_removed,
        },
        now=now,
    )
    return report
