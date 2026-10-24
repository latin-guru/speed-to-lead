"""Checks that run before every automated send.

Each guard answers "may this go now?" and, if not, *when* it may. A "later" becomes a
deferred job, so nothing is dropped:

- quiet hours in the lead's local time (business timezone by default)
- "one voice" frequency limits per person: at most N touches in a rolling 7 days, and a
  minimum gap between touches (transactional replies skip the gap)
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


# --------------------------------------------------------------------------- quiet hours


def lead_zone(lead_tz: str | None, default_tz: str) -> ZoneInfo:
    if lead_tz:
        try:
            return ZoneInfo(lead_tz)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return ZoneInfo(default_tz)


def _in_window(local: datetime, start: int, end: int) -> bool:
    """True if ``local`` is inside allowed sending hours [end, start).

    ``start``/``end`` describe the *quiet* period, e.g. 21 -> 8 means quiet from 21:00 until
    08:00. A quiet period that does not cross midnight (e.g. 12 -> 13) also works.
    """
    hour = local.hour
    if start > end:  # quiet period crosses midnight
        return end <= hour < start
    return not (start <= hour < end)


def next_send_time(now: datetime, zone: ZoneInfo, *, quiet_start: int, quiet_end: int) -> datetime:
    """``now`` if sending is allowed now, else the next moment quiet hours end (UTC)."""
    local = now.astimezone(zone)
    if _in_window(local, quiet_start, quiet_end):
        return now
    day = local.date()
    if quiet_start > quiet_end and local.hour >= quiet_start:
        day += timedelta(days=1)  # evening: quiet ends tomorrow morning
    opens = datetime.combine(day, time(hour=quiet_end), tzinfo=zone)
    return utc(opens)


# --------------------------------------------------------------------------- frequency


@dataclass(frozen=True)
class FrequencyDecision:
    allowed: bool
    not_before: datetime | None = None
    reason: str = ""


def _person_touch_times(session: Session, lead: Lead, since: datetime) -> list[datetime]:
    """Send times to *this person* (any lead sharing the phone or email) after ``since``."""
    same_person = [Lead.id == lead.id]
    if lead.phone:
        same_person.append(Lead.phone == lead.phone)
    if lead.email:
        same_person.append(Lead.email == lead.email)
    lead_ids = select(Lead.id).where(or_(*same_person))
    rows = session.scalars(
        select(OutboxMessage.sent_at)
        .where(OutboxMessage.lead_id.in_(lead_ids), OutboxMessage.sent_at > since)
        .order_by(OutboxMessage.sent_at)
    )
    return [utc(t) for t in rows]


def check_frequency(
    session: Session,
    lead: Lead,
    *,
    now: datetime,
    max_per_week: int,
    min_gap: timedelta,
    transactional: bool,
) -> FrequencyDecision:
    touches = _person_touch_times(session, lead, now - WEEK)
    if len(touches) >= max_per_week:
        # A slot frees up when the oldest touch in the window ages out.
        oldest_counted = touches[len(touches) - max_per_week]
        return FrequencyDecision(False, oldest_counted + WEEK, "weekly_cap")
    if not transactional and touches and now - touches[-1] < min_gap:
        return FrequencyDecision(False, touches[-1] + min_gap, "min_gap")
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
