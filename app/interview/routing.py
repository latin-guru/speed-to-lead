"""Which sender a message goes to: the real email sender only for an invite's one email.

The follow-up engine asks ``InterviewEmailRouter`` after every guard has passed. The real
sender is used only when ALL of these hold; otherwise the mock sender the engine already
picked is kept:

1. the message is an email (SMS is always the mock);
2. the lead came from an invite link (source ``interview_demo`` AND an invite row points at
   it; the public form and API can never create that row);
3. it is the instant acknowledgment (a transactional step), not a later reminder;
4. real email is configured (``EMAIL_BACKEND`` != mock and ``INTERVIEW_EMAIL_ENABLED``);
5. this invite has not sent an email yet, and the caps allow it: per hour, per day, and one
   per recipient per day (rolling windows, counted from the outbox).

When a cap stops it, the invite records ``mock:<reason>`` so the visitor's panel can say
"demo limit reached".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.followups.senders import MessageSender, OutboundMessage
from app.followups.sequence import Channel
from app.interview import INTERVIEW_SOURCE
from app.interview.mailer import REAL_SENDERS, RealEmailSender
from app.models import InterviewInvite, Lead, OutboxMessage

log = logging.getLogger(__name__)

SENT = "sent"
LIMIT_REASONS = frozenset({"limit_hour", "limit_day", "limit_recipient", "limit_invite"})


@dataclass(frozen=True)
class EmailCaps:
    per_hour: int
    per_day: int


def real_sent_since(session: Session, since: datetime) -> int:
    stmt = select(func.count(OutboxMessage.id)).where(
        OutboxMessage.sender.in_(REAL_SENDERS), OutboxMessage.sent_at >= since
    )
    return int(session.scalar(stmt) or 0)


def real_sent_to(session: Session, email_bidx: str, since: datetime) -> int:
    stmt = (
        select(func.count(OutboxMessage.id))
        .join(Lead, Lead.id == OutboxMessage.lead_id)
        .where(
            OutboxMessage.sender.in_(REAL_SENDERS),
            OutboxMessage.sent_at >= since,
            Lead.email_bidx == email_bidx,
        )
    )
    return int(session.scalar(stmt) or 0)


def cap_reason(
    session: Session, invite: InterviewInvite, lead: Lead, caps: EmailCaps, now: datetime
) -> str | None:
    if invite.email_sent_at is not None or invite.email_result == SENT:
        return "limit_invite"
    if real_sent_since(session, now - timedelta(hours=1)) >= caps.per_hour:
        return "limit_hour"
    if real_sent_since(session, now - timedelta(days=1)) >= caps.per_day:
        return "limit_day"
    if lead.email_bidx and real_sent_to(session, lead.email_bidx, now - timedelta(days=1)):
        return "limit_recipient"
    return None


class InterviewEmailRouter:
    def __init__(self, real: RealEmailSender | None, caps: EmailCaps) -> None:
        self.real = real
        self.caps = caps

    def __call__(
        self,
        session: Session,
        lead: Lead,
        message: OutboundMessage,
        default: MessageSender,
        now: datetime,
    ) -> MessageSender:
        if message.channel is not Channel.EMAIL or lead.source != INTERVIEW_SOURCE:
            return default
        invite = session.scalar(select(InterviewInvite).where(InterviewInvite.lead_id == lead.id))
        if invite is None or not message.transactional:
            return default
        if self.real is None:
            invite.email_result = "mock:disabled"
            return default
        reason = cap_reason(session, invite, lead, self.caps, now)
        if reason is not None:
            if invite.email_result != SENT:
                invite.email_result = f"mock:{reason}"
            log.info("interview_email_capped", extra={"invite_id": invite.id, "reason": reason})
            return default
        # Rolled back with the job if delivery fails, so a retry sees the invite unsent.
        invite.email_result = SENT
        invite.email_sent_at = now
        return self.real
