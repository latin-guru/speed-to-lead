"""What each job kind does. Handlers run inside the worker's transaction and never commit."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.ai.service import ScoringService
from app.crm.base import CRMAdapter
from app.crm.sync import run_crm_sync
from app.followups.engine import SendPolicy, send_step
from app.followups.senders import MessageSender, SenderPicker
from app.followups.sequence import Channel, Sequence
from app.jobs import kinds
from app.jobs.queue import ClaimedJob, PermanentJobError
from app.services.scoring import score_lead


@dataclass(frozen=True)
class JobContext:
    scoring: ScoringService
    sequence: Sequence
    senders: dict[Channel, MessageSender]
    policy: SendPolicy
    crm: CRMAdapter | None
    # Interview mode: may route an invite's one email to the real sender (else None).
    pick_sender: SenderPicker | None = None


Handler = Callable[[JobContext, Session, ClaimedJob, datetime], str]


def _lead_id(job: ClaimedJob) -> uuid.UUID:
    try:
        return uuid.UUID(str(job.payload["lead_id"]))
    except (KeyError, ValueError) as exc:
        raise PermanentJobError("payload needs a valid lead_id") from exc


def handle_score_lead(ctx: JobContext, session: Session, job: ClaimedJob, now: datetime) -> str:
    return score_lead(session, _lead_id(job), ctx.scoring, now=now)


def handle_followup_send(ctx: JobContext, session: Session, job: ClaimedJob, now: datetime) -> str:
    step = job.payload.get("step")
    if not isinstance(step, str):
        raise PermanentJobError("payload needs a step")
    return send_step(
        session,
        _lead_id(job),
        step,
        sequence=ctx.sequence,
        senders=ctx.senders,
        policy=ctx.policy,
        now=now,
        pick_sender=ctx.pick_sender,
    )


def handle_crm_sync(ctx: JobContext, session: Session, job: ClaimedJob, now: datetime) -> str:
    if ctx.crm is None:
        return "skipped:crm_disabled"
    return run_crm_sync(session, _lead_id(job), ctx.crm, now=now)


HANDLERS: dict[str, Handler] = {
    kinds.SCORE_LEAD: handle_score_lead,
    kinds.FOLLOWUP_SEND: handle_followup_send,
    kinds.CRM_SYNC: handle_crm_sync,
}
