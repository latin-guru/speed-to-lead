"""Pipeline stages and the only code path that changes ``Lead.stage``.

Rules:

- Moves follow ``ALLOWED_TRANSITIONS``; anything else is rejected.
- ``lost`` needs a reason. ``won`` is final.
- A ``lost`` lead can be reopened to ``qualified`` (an operator action, with a required
  reason). The reopen is logged as its own ``lead_reopened`` activity; automation stays
  stopped (a human is working the lead).
- Only a human (``actor="human:<name>"``) can move a stage. The scorer writes a
  *suggested* ``next_step`` and nothing else; there is deliberately no code path from a
  score to a stage change.
- Moving to contacted, won or lost stops the follow-up sequence and queues a CRM sync.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.crm.sync import enqueue_crm_sync
from app.followups.engine import STOPPING_STAGES, record_first_response, stop_sequence
from app.models import Activity, Lead, LeadStage
from app.services.timeline import add_activity

ALLOWED_TRANSITIONS: dict[LeadStage, frozenset[LeadStage]] = {
    LeadStage.NEW: frozenset({LeadStage.QUALIFIED, LeadStage.CONTACTED, LeadStage.LOST}),
    LeadStage.QUALIFIED: frozenset({LeadStage.CONTACTED, LeadStage.WON, LeadStage.LOST}),
    LeadStage.CONTACTED: frozenset({LeadStage.WON, LeadStage.LOST}),
    LeadStage.WON: frozenset(),
    LeadStage.LOST: frozenset({LeadStage.QUALIFIED}),  # reopen, reason required
}

HUMAN_PREFIX = "human:"


class TransitionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def can_move(current: LeadStage, target: LeadStage) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


def move_stage(
    session: Session,
    lead: Lead,
    target: LeadStage,
    *,
    actor: str,
    now: datetime,
    reason: str | None = None,
    max_attempts: int = 5,
    crm_enabled: bool = True,
) -> Activity:
    """Validate and apply a stage move. The caller commits."""
    if not actor.startswith(HUMAN_PREFIX) or len(actor) == len(HUMAN_PREFIX):
        raise TransitionError("human_only", "only a human can change a lead's stage")
    current = LeadStage(lead.stage)
    if target == current:
        raise TransitionError("same_stage", f"lead is already {current.value}")
    if not can_move(current, target):
        allowed = sorted(s.value for s in ALLOWED_TRANSITIONS[current])
        raise TransitionError(
            "invalid_transition",
            f"cannot move from {current.value} to {target.value} (allowed: {allowed or 'none'})",
        )
    reason = (reason or "").strip() or None
    if target == LeadStage.LOST and not reason:
        raise TransitionError("reason_required", "a reason is required to mark a lead lost")
    reopening = current == LeadStage.LOST
    if reopening and not reason:
        raise TransitionError("reason_required", "a reason is required to reopen a lost lead")

    lead.stage = target.value
    previous_lost_reason = lead.lost_reason
    if target == LeadStage.LOST:
        lead.lost_reason = reason
    if reopening:
        lead.lost_reason = None
    activity = add_activity(
        session,
        lead,
        "stage_changed",
        actor,
        {"from": current.value, "to": target.value, "reason": reason},
    )
    if reopening:
        add_activity(
            session,
            lead,
            "lead_reopened",
            actor,
            {"reason": reason, "previous_lost_reason": previous_lost_reason},
        )
    if target == LeadStage.CONTACTED:
        record_first_response(lead, now)
    if target in STOPPING_STAGES:
        stop_sequence(session, lead, f"stage_{target.value}", now=now)
    enqueue_crm_sync(session, lead, max_attempts=max_attempts, now=now, enabled=crm_enabled)
    return activity


def log_human_contact(
    session: Session, lead: Lead, *, actor: str, channel: str, now: datetime, note: str | None
) -> Activity:
    """A person reached the lead (call, text, email). Counts as first response and takes
    the lead out of automation, without forcing a stage change."""
    if not actor.startswith(HUMAN_PREFIX):
        raise TransitionError("human_only", "only a human can log contact")
    record_first_response(lead, now)
    activity = add_activity(
        session, lead, "human_contact", actor, {"channel": channel, "note": note}
    )
    stop_sequence(session, lead, "contacted_by_human", now=now)
    return activity
