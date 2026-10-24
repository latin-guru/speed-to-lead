"""Follow-up orchestration: enqueue a sequence, send one step behind every guard, and stop,
pause or opt out a lead. None of these functions commit; the caller (API route or job) owns
the transaction.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.followups.guards import (
    SendWindow,
    check_frequency,
    lead_zone,
    next_send_time,
    outside_window,
    sends_paused,
)
from app.followups.senders import MessageSender, OutboundMessage
from app.followups.sequence import Channel, Sequence, render
from app.followups.wording import call_phrase, received_phrase
from app.jobs import kinds
from app.jobs.queue import Deferred, PermanentJobError, cancel_pending, enqueue
from app.models import Lead, LeadStage, OutboxMessage, SequenceState
from app.services.timeline import add_activity

log = logging.getLogger(__name__)

STOP_KEYWORDS = frozenset({"stop", "stopall", "unsubscribe", "cancel", "end", "quit"})
# A human has the lead, or it is closed: automation stops for good.
STOPPING_STAGES = frozenset({LeadStage.CONTACTED, LeadStage.WON, LeadStage.LOST})


@dataclass(frozen=True)
class SendPolicy:
    company: str
    callback_minutes: int
    business_timezone: str
    quiet_hours: bool  # enforce the sending window
    window: SendWindow
    max_per_week: int
    min_gap: timedelta
    env_paused: bool
    kill_switch_recheck: timedelta


def enqueue_sequence(
    session: Session, lead: Lead, sequence: Sequence, *, max_attempts: int, now: datetime
) -> int:
    """Queue every step of ``sequence`` for a new lead. Returns how many were queued."""
    queued = 0
    for step in sequence.steps:
        queued += enqueue(
            session,
            kinds.FOLLOWUP_SEND,
            {"lead_id": str(lead.id), "step": step.id, "sequence": sequence.name},
            idempotency_key=kinds.followup_key(lead.id, step.id),
            run_at=lead.created_at + timedelta(minutes=step.delay_minutes),
            max_attempts=max_attempts,
            lead_id=lead.id,
            now=now,
        )
    return queued


def _halt(session: Session, lead: Lead, state: SequenceState, reason: str, now: datetime) -> int:
    cancelled = cancel_pending(session, lead.id, kinds.FOLLOWUP_SEND, reason, now=now)
    if lead.sequence_state != state.value or cancelled:
        previous = lead.sequence_state
        lead.sequence_state = state.value
        add_activity(
            session,
            lead,
            f"sequence_{state.value}",
            "system",
            {"reason": reason, "cancelled_sends": cancelled, "previous": previous},
        )
    return cancelled


def stop_sequence(session: Session, lead: Lead, reason: str, *, now: datetime) -> int:
    """Stop automation for good (closed, contacted by a human, opted out)."""
    return _halt(session, lead, SequenceState.STOPPED, reason, now)


def pause_sequence(session: Session, lead: Lead, reason: str, *, now: datetime) -> int:
    """Hand off to a human: cancel pending sends; a human decides what happens next."""
    if lead.sequence_state == SequenceState.STOPPED.value:
        return _halt(session, lead, SequenceState.STOPPED, reason, now)
    return _halt(session, lead, SequenceState.PAUSED, reason, now)


def is_stop_keyword(body: str) -> bool:
    return body.strip().strip(".!").lower() in STOP_KEYWORDS


def opt_out(session: Session, phone: str, *, now: datetime, source: str) -> list[Lead]:
    """Opt out every lead with this phone number and cancel their pending sends."""
    leads = list(session.scalars(select(Lead).where(Lead.phone == phone)))
    for lead in leads:
        if lead.opted_out_at is None:
            lead.opted_out_at = now
            add_activity(session, lead, "opted_out", source, {"channel": "sms"})
        stop_sequence(session, lead, "opted_out", now=now)
    return leads


def record_first_response(lead: Lead, now: datetime) -> None:
    """First time anyone (automation or a human) actually reached the lead."""
    if lead.first_response_at is None:
        lead.first_response_at = now


def send_step(
    session: Session,
    lead_id: uuid.UUID,
    step_id: str,
    *,
    sequence: Sequence,
    senders: dict[Channel, MessageSender],
    policy: SendPolicy,
    now: datetime,
) -> str:
    """Try to send one step. Returns what happened; raises ``Deferred`` for "not yet".

    Order matters: the cheap "should this ever go?" checks come first, then the "not now"
    guards, and the idempotent send last.
    """
    step = sequence.step(step_id)
    if step is None:
        raise PermanentJobError(f"unknown step {step_id!r} in sequence {sequence.name!r}")
    lead = session.get(Lead, lead_id)
    if lead is None:
        return "skipped:lead_missing"

    key = kinds.send_key(lead.id, step.id)
    if session.scalar(select(OutboxMessage.id).where(OutboxMessage.idempotency_key == key)):
        return "already_sent"

    stop_reason = _stop_reason(lead, step.channel)
    if stop_reason:
        add_activity(
            session, lead, "followup_skipped", "system", {"step": step.id, "reason": stop_reason}
        )
        return f"skipped:{stop_reason}"

    if sends_paused(session, env_paused=policy.env_paused):
        raise Deferred(now + policy.kill_switch_recheck, "kill_switch")

    zone = lead_zone(lead.timezone, policy.business_timezone)
    if policy.quiet_hours:
        opens = next_send_time(now, zone, policy.window)
        if opens > now:
            raise Deferred(opens, "quiet_hours")

    freq = check_frequency(
        session,
        lead,
        now=now,
        max_per_week=policy.max_per_week,
        min_gap=policy.min_gap,
        transactional=step.transactional,
    )
    if not freq.allowed:
        assert freq.not_before is not None
        raise Deferred(freq.not_before, freq.reason)

    to_address = lead.phone if step.channel is Channel.SMS else lead.email
    assert to_address is not None  # _stop_reason checked it

    def fill(template: str) -> str:
        return render(
            template,
            name=lead.name,
            company=policy.company,
            callback_minutes=policy.callback_minutes,
            received_when=received_phrase(lead.created_at, now, zone),
            call_when=call_phrase(now, zone),
        )

    # Arrived outside the window: this send is the window opening, so use the variant that
    # says when we will call instead of "within N minutes".
    after_hours = (
        policy.quiet_hours
        and step.after_hours_body is not None
        and outside_window(lead.created_at, zone, policy.window)
    )
    body = step.after_hours_body if after_hours and step.after_hours_body else step.body
    subject = step.after_hours_subject if after_hours and step.after_hours_subject else step.subject

    message = OutboundMessage(
        lead_id=lead.id,
        step=step.id,
        channel=step.channel,
        to_address=to_address,
        subject=fill(subject) if subject else None,
        body=fill(body),
        idempotency_key=key,
        transactional=step.transactional,
    )
    sender = senders[step.channel]
    if not sender.send(session, message, now=now):
        return "already_sent"  # lost a race with a duplicate run of this job
    record_first_response(lead, now)
    add_activity(
        session,
        lead,
        "followup_sent",
        f"system:{sender.name}",
        {
            "step": step.id,
            "channel": step.channel.value,
            "sender": sender.name,
            "variant": "after_hours" if after_hours else "standard",
        },
    )
    log.info(
        "followup_sent",
        extra={"lead_id": str(lead.id), "step": step.id, "channel": step.channel.value},
    )
    return "sent"


def _stop_reason(lead: Lead, channel: Channel) -> str:
    if lead.opted_out_at is not None:
        return "opted_out"
    if lead.stage in STOPPING_STAGES:
        return f"stage_{lead.stage}"
    if lead.sequence_state != SequenceState.ACTIVE.value:
        return f"sequence_{lead.sequence_state}"
    if channel is Channel.SMS and not lead.phone:
        return "no_phone"
    if channel is Channel.EMAIL and not lead.email:
        return "no_email"
    return ""
