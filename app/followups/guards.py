"""Checks that run before every automated send.

Each guard answers "may this go now?" and, if not, *when* it may. A "later" becomes a
deferred job, so nothing is dropped:

- the sending window in the lead's local time (business timezone by default)
- "one voice" frequency limits per person: at most N touches in a rolling 7 days, and a
  minimum gap between touches. The instant acknowledgment (SMS + email for one lead) is
  *one* touch and skips the gap.
- the global kill switch (env ``SENDS_PAUSED`` or the ``sends_paused`` DB flag)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db import utc
from app.models import Lead, OutboxMessage, SystemFlag, utcnow

WEEK = timedelta(days=7)
KILL_SWITCH_FLAG = "sends_paused"


# --------------------------------------------------------------------------- sending window

DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DEFAULT_SEND_WINDOWS = "mon-sat 09:00-20:00; sun 15:00-18:00"


@dataclass(frozen=True)
class SendWindow:
    """Allowed sending hours per weekday, in the lead's local time.

    Parsed from a spec like ``"mon-sat 09:00-20:00; sun 15:00-18:00"``: ``;``-separated
    rules, each a day, a day range (``mon-fri``) or a comma list (``sat,sun``) followed by
    ``HH:MM-HH:MM`` (end exclusive). A day with no rule is closed. Later rules override
    earlier ones for the same day.
    """

    hours: tuple[tuple[time, time] | None, ...]  # index 0 = Monday

    @classmethod
    def parse(cls, spec: str) -> SendWindow:
        hours: list[tuple[time, time] | None] = [None] * 7
        rules = [r.strip() for r in spec.split(";") if r.strip()]
        if not rules:
            raise ValueError("send window spec is empty")
        for rule in rules:
            try:
                days_part, span = rule.split()
                start_s, end_s = span.split("-")
                start, end = time.fromisoformat(start_s), time.fromisoformat(end_s)
            except ValueError as exc:
                raise ValueError(f"bad send window rule {rule!r}") from exc
            if len(start_s) != 5 or len(end_s) != 5 or start >= end:
                raise ValueError(f"bad send window hours in {rule!r} (use HH:MM-HH:MM)")
            for day in _parse_days(days_part.lower()):
                hours[day] = (start, end)
        return cls(tuple(hours))


def _parse_days(part: str) -> list[int]:
    days: list[int] = []
    for item in part.split(","):
        first, _, last = item.partition("-")
        if first not in DAY_NAMES or (last and last not in DAY_NAMES):
            raise ValueError(f"unknown day in {part!r} (use mon..sun)")
        lo, hi = DAY_NAMES.index(first), DAY_NAMES.index(last or first)
        if hi < lo:
            raise ValueError(f"day range {item!r} runs backwards")
        days.extend(range(lo, hi + 1))
    return days


def lead_zone(lead_tz: str | None, default_tz: str) -> ZoneInfo:
    if lead_tz:
        try:
            return ZoneInfo(lead_tz)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return ZoneInfo(default_tz)


def next_send_time(now: datetime, zone: ZoneInfo, window: SendWindow) -> datetime:
    """``now`` if it is inside the sending window, else the next time the window opens (UTC).

    Window edges are built as local wall-clock times on each day, so a DST change between
    "now" and the opening is handled (09:00 local stays 09:00 local).
    """
    local_day = now.astimezone(zone).date()
    for offset in range(8):
        day = local_day + timedelta(days=offset)
        span = window.hours[day.weekday()]
        if span is None:
            continue
        opens = utc(datetime.combine(day, span[0], tzinfo=zone))
        closes = utc(datetime.combine(day, span[1], tzinfo=zone))
        if opens <= now < closes:
            return now
        if now < opens:
            return opens
    raise ValueError("send window has no open hours")  # SendWindow.parse rules this out


def outside_window(at: datetime, zone: ZoneInfo, window: SendWindow) -> bool:
    return next_send_time(at, zone, window) > at


# --------------------------------------------------------------------------- frequency


@dataclass(frozen=True)
class FrequencyDecision:
    allowed: bool
    not_before: datetime | None = None
    reason: str = ""


def _person_touches(session: Session, lead: Lead, since: datetime) -> list[tuple[datetime, bool]]:
    """Touches to *this person* (any lead sharing the phone or email) after ``since``.

    Returns ``(time, counted_for_this_lead_ack)`` pairs, oldest first. A non-transactional
    message is one touch. All transactional messages for one lead (the instant SMS + email
    acknowledgment) together are a single touch, timed at the first of them.
    """
    same_person = [Lead.id == lead.id]
    if lead.phone_bidx:  # blind indexes: compare without decrypting
        same_person.append(Lead.phone_bidx == lead.phone_bidx)
    if lead.email_bidx:
        same_person.append(Lead.email_bidx == lead.email_bidx)
    lead_ids = select(Lead.id).where(or_(*same_person))
    rows = session.execute(
        select(OutboxMessage.lead_id, OutboxMessage.sent_at, OutboxMessage.transactional)
        .where(OutboxMessage.lead_id.in_(lead_ids), OutboxMessage.sent_at > since)
        .order_by(OutboxMessage.sent_at)
    )
    touches: list[tuple[datetime, bool]] = []
    acked: set[object] = set()
    for lead_id, sent_at, transactional in rows:
        if transactional:
            if lead_id in acked:
                continue
            acked.add(lead_id)
        touches.append((utc(sent_at), bool(transactional) and lead_id == lead.id))
    return touches


def check_frequency(
    session: Session,
    lead: Lead,
    *,
    now: datetime,
    max_per_week: int,
    min_gap: timedelta,
    transactional: bool,
) -> FrequencyDecision:
    touches = _person_touches(session, lead, now - WEEK)
    if transactional and any(own_ack for _, own_ack in touches):
        return FrequencyDecision(True)  # second half of this lead's ack: same touch
    times = [t for t, _ in touches]
    if len(times) >= max_per_week:
        # A slot frees up when the oldest touch in the window ages out.
        oldest_counted = times[len(times) - max_per_week]
        return FrequencyDecision(False, oldest_counted + WEEK, "weekly_cap")
    if not transactional and times and now - times[-1] < min_gap:
        return FrequencyDecision(False, times[-1] + min_gap, "min_gap")
    return FrequencyDecision(True)


# --------------------------------------------------------------------------- kill switch


def sends_paused(session: Session, *, env_paused: bool) -> bool:
    if env_paused:
        return True
    flag = session.get(SystemFlag, KILL_SWITCH_FLAG)
    return flag is not None and flag.value == "true"


def set_sends_paused(
    session: Session, paused: bool, *, actor: str, now: datetime | None = None
) -> SystemFlag:
    """Flip the DB kill switch. The caller commits."""
    moment = now or utcnow()
    flag = session.get(SystemFlag, KILL_SWITCH_FLAG)
    if flag is None:
        flag = SystemFlag(key=KILL_SWITCH_FLAG, value="false", updated_by=actor)
        session.add(flag)
    flag.value = "true" if paused else "false"
    flag.updated_by = actor
    flag.updated_at = moment
    return flag
