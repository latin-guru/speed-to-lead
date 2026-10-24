"""The visitor's live panel: what happened to the lead they just created, step by step.

Polled as JSON by ``/static/try.js``. It holds only what the visitor typed or what the
pipeline did with it; the email address is masked.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.interview.mailer import REAL_SENDERS, mask_email
from app.jobs import kinds
from app.models import InterviewInvite, Job, JobStatus, Lead, OutboxMessage

NOTES = {
    "mock:disabled": "Real email is turned off on this server, so the mock sender recorded "
    "it instead.",
    "mock:limit_hour": "Demo limit reached: the mock sender recorded it instead of sending it.",
    "mock:limit_day": "Demo limit reached: the mock sender recorded it instead of sending it.",
    "mock:limit_recipient": "Demo limit reached for this address today: the mock sender "
    "recorded it instead of sending it.",
    "mock:limit_invite": "This link already sent its one email.",
}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _email_step(session: Session, invite: InterviewInvite, lead: Lead) -> dict[str, Any]:
    msg = session.scalars(
        select(OutboxMessage)
        .where(OutboxMessage.lead_id == lead.id, OutboxMessage.channel == "email")
        .order_by(OutboxMessage.id)
        .limit(1)
    ).first()
    step: dict[str, Any] = {"key": "email", "label": "Email reply sent", "done": False}
    if msg is not None:
        real = msg.sender in REAL_SENDERS
        step.update(
            done=True,
            at=_iso(msg.sent_at),
            real=real,
            to=mask_email(msg.to_address),
            sender=msg.sender,
            detail=(
                f"Sent for real to {mask_email(msg.to_address)}. Check your inbox (and spam)."
                if real
                else "Recorded by the mock email sender (nothing was delivered)."
            ),
        )
        note = NOTES.get(invite.email_result or "")
        if not real and note:
            step["note"] = note
        return step
    dead = session.scalar(
        select(Job.id).where(
            Job.lead_id == lead.id,
            Job.kind == kinds.FOLLOWUP_SEND,
            Job.status == JobStatus.DEAD.value,
        )
    )
    if dead is not None:
        step.update(failed=True, detail="The email could not be delivered.")
    return step


def panel(session: Session, invite: InterviewInvite, *, crm_enabled: bool = True) -> dict[str, Any]:
    lead = session.get(Lead, invite.lead_id) if invite.lead_id is not None else None
    if lead is None:
        return {"steps": [], "complete": True, "gone": True}
    steps: list[dict[str, Any]] = [
        {
            "key": "created",
            "label": "Lead created",
            "done": True,
            "at": _iso(lead.created_at),
            "detail": "Captured through the real pipeline (source: interview_demo).",
        },
        {
            "key": "scored",
            "label": "Scored",
            "done": lead.scored_at is not None,
            "at": _iso(lead.scored_at),
            "score": lead.score,
            "summary": lead.score_summary,
            "detail": f"Scored by the {lead.scored_by} scorer." if lead.scored_by else None,
        },
        _email_step(session, invite, lead),
        {
            "key": "crm",
            "label": "CRM synced",
            "done": lead.crm_synced_at is not None or not crm_enabled,
            "at": _iso(lead.crm_synced_at),
            "detail": (
                "Synced to the mock CRM."
                if lead.crm_synced_at
                else (None if crm_enabled else "CRM sync is turned off on this server.")
            ),
        },
    ]
    complete = all(s["done"] or s.get("failed") for s in steps)
    return {
        "steps": steps,
        "complete": complete,
        "sms": "Texts are never sent in interview mode.",
    }
