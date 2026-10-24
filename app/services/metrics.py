"""Dashboard metrics. Each function is one SQL query (or two) over a date range.

The SQL is portable between SQLite and Postgres:

- **Response time** is ``first_response_at - created_at`` in seconds, built per dialect
  (``julianday`` on SQLite, ``EXTRACT(EPOCH ...)`` on Postgres).
- **Median** uses ``ORDER BY ... LIMIT/OFFSET`` around the middle row(s), which both
  databases run as-is (neither ``percentile_cont`` nor ``MEDIAN`` is portable).
- **Funnel** ranks how far each lead got: the furthest of its current stage and every
  stage it was ever moved to (``stage_changed`` activities). A lead that went
  new -> contacted -> lost still counts as having reached ``qualified`` and ``contacted``.
  ``lost`` is not a funnel step; it is reported next to the funnel.

All ranges are half-open: ``start <= created_at < end``, by lead creation time (cohorts).
"""

from __future__ import annotations

import statistics
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import (
    ColumnElement,
    Float,
    Select,
    case,
    cast,
    extract,
    func,
    literal,
    select,
    union_all,
)
from sqlalchemy.orm import Session

from app.db import utc
from app.models import Activity, Job, JobStatus, Lead, LeadStage, Task, TaskStatus

FUNNEL = (LeadStage.NEW, LeadStage.QUALIFIED, LeadStage.CONTACTED, LeadStage.WON)
SCORE_BUCKETS = 10  # 0-9, 10-19, ..., 90-100


@dataclass(frozen=True)
class Period:
    start: datetime
    end: datetime

    @classmethod
    def last_days(cls, now: datetime, days: int) -> Period:
        return cls(now - timedelta(days=days), now)


def _in(period: Period) -> list[ColumnElement[bool]]:
    return [Lead.created_at >= period.start, Lead.created_at < period.end]


# --------------------------------------------------------------------------- response time


def response_seconds(dialect: str) -> ColumnElement[float]:
    """``first_response_at - created_at`` in seconds, for this SQL dialect."""
    if dialect == "sqlite":
        days = func.julianday(Lead.first_response_at) - func.julianday(Lead.created_at)
        return cast(days * 86400.0, Float)
    return cast(extract("epoch", Lead.first_response_at - Lead.created_at), Float)


def _dialect(session: Session) -> str:
    return session.get_bind().dialect.name


def _responded(session: Session, period: Period) -> Select[Any]:
    secs = response_seconds(_dialect(session)).label("secs")
    return select(secs).where(*_in(period), Lead.first_response_at.is_not(None))


def median_response_seconds(session: Session, period: Period) -> float | None:
    """Median seconds from capture to first response, over leads that got one."""
    rows = _responded(session, period).subquery()
    n = int(session.scalar(select(func.count()).select_from(rows)) or 0)
    if n == 0:
        return None
    middle = (
        select(rows.c.secs)
        .order_by(rows.c.secs)
        .limit(1 if n % 2 else 2)
        .offset((n - 1) // 2)
        .subquery()
    )
    value = session.scalar(select(func.avg(middle.c.secs)))
    return round(float(value), 3) if value is not None else None


def response_trend(
    session: Session, period: Period, zone: ZoneInfo
) -> list[tuple[date, float | None, int]]:
    """Per local day in ``period``: (day, median seconds or None, leads responded to)."""
    secs = response_seconds(_dialect(session)).label("secs")
    rows = session.execute(
        select(Lead.created_at, secs).where(*_in(period), Lead.first_response_at.is_not(None))
    )
    by_day: dict[date, list[float]] = defaultdict(list)
    for created_at, value in rows:
        by_day[utc(created_at).astimezone(zone).date()].append(float(value))
    first = period.start.astimezone(zone).date()
    last = (period.end - timedelta(microseconds=1)).astimezone(zone).date()
    out: list[tuple[date, float | None, int]] = []
    day = first
    while day <= last:
        values = by_day.get(day, [])
        out.append((day, round(statistics.median(values), 3) if values else None, len(values)))
        day += timedelta(days=1)
    return out


# --------------------------------------------------------------------------- funnel


def _stage_rank(column: ColumnElement[Any]) -> ColumnElement[int]:
    return case({s.value: i for i, s in enumerate(FUNNEL)}, value=column, else_=0)


def furthest_stage_counts(session: Session, period: Period) -> dict[int, int]:
    """How many leads (created in ``period``) got at most this far: {rank: leads}."""
    moved_to = Activity.detail["to"].as_string()
    events = union_all(
        select(Activity.lead_id.label("lead_id"), moved_to.label("stage")).where(
            Activity.kind == "stage_changed"
        ),
        select(Lead.id.label("lead_id"), Lead.stage.label("stage")),
    ).subquery()
    per_lead = (
        select(events.c.lead_id, func.max(_stage_rank(events.c.stage)).label("rank"))
        .join(Lead, Lead.id == events.c.lead_id)
        .where(*_in(period))
        .group_by(events.c.lead_id)
        .subquery()
    )
    rows = session.execute(select(per_lead.c.rank, func.count()).group_by(per_lead.c.rank))
    return {int(rank): int(count) for rank, count in rows}


@dataclass(frozen=True)
class FunnelStep:
    stage: str
    reached: int
    conversion: float | None  # share of the previous step that reached this one


def funnel(session: Session, period: Period) -> list[FunnelStep]:
    counts = furthest_stage_counts(session, period)
    steps: list[FunnelStep] = []
    previous: int | None = None
    for i, stage in enumerate(FUNNEL):
        reached = sum(c for rank, c in counts.items() if rank >= i)
        rate = None if previous is None else (reached / previous if previous else None)
        steps.append(FunnelStep(stage.value, reached, None if rate is None else round(rate, 4)))
        previous = reached
    return steps


def stage_counts(session: Session, period: Period) -> dict[str, int]:
    rows = session.execute(
        select(Lead.stage, func.count()).where(*_in(period)).group_by(Lead.stage)
    )
    counts = {s.value: 0 for s in LeadStage}
    counts.update({stage: int(n) for stage, n in rows})
    return counts


def win_rate(session: Session, period: Period) -> float | None:
    """New -> Won: share of leads created in ``period`` that are won now."""
    total = int(session.scalar(select(func.count(Lead.id)).where(*_in(period))) or 0)
    if total == 0:
        return None
    won = int(
        session.scalar(
            select(func.count(Lead.id)).where(*_in(period), Lead.stage == LeadStage.WON.value)
        )
        or 0
    )
    return round(won / total, 4)


# --------------------------------------------------------------------------- the rest


def lead_count(session: Session, period: Period) -> int:
    return int(session.scalar(select(func.count(Lead.id)).where(*_in(period))) or 0)


def average_score(session: Session, period: Period) -> float | None:
    value = session.scalar(select(func.avg(Lead.score)).where(*_in(period)))
    return round(float(value), 1) if value is not None else None


def open_task_count(session: Session) -> int:
    stmt = select(func.count(Task.id)).where(Task.status == TaskStatus.OPEN.value)
    return int(session.scalar(stmt) or 0)


def dead_job_count(session: Session) -> int:
    stmt = select(func.count(Job.id)).where(Job.status == JobStatus.DEAD.value)
    return int(session.scalar(stmt) or 0)


def leads_by_source(session: Session, period: Period) -> list[tuple[str, int]]:
    n = func.count(Lead.id).label("n")
    rows = session.execute(
        select(Lead.source, n)
        .where(*_in(period))
        .group_by(Lead.source)
        .order_by(n.desc(), Lead.source)
    )
    return [(source, int(count)) for source, count in rows]


def score_histogram(session: Session, period: Period) -> list[tuple[str, int]]:
    """Scored leads per 10-point bucket; 100 joins the 90-100 bucket."""
    bucket = case((Lead.score >= 90, literal(9)), else_=Lead.score // 10).label("bucket")
    rows = session.execute(
        select(bucket, func.count()).where(*_in(period), Lead.score.is_not(None)).group_by(bucket)
    )
    counts = {int(b): int(c) for b, c in rows}
    labels = [f"{i * 10}-{i * 10 + 9}" for i in range(SCORE_BUCKETS - 1)] + ["90-100"]
    return [(label, counts.get(i, 0)) for i, label in enumerate(labels)]


@dataclass(frozen=True)
class ActivityRow:
    id: int
    lead_id: uuid.UUID
    lead_name: str
    kind: str
    actor: str
    created_at: datetime
    summary: str


def activity_summary(kind: str, detail: dict[str, Any]) -> str:
    """A short, PII-free line per activity kind. Free text (replies, notes) is left out."""
    d = detail or {}
    texts: dict[str, str] = {
        "lead_created": f"New lead from {d.get('source', '?')}",
        "ai_scored": (
            f"Scored {d.get('score')} by {d.get('scored_by')}, suggests {d.get('next_step')}"
        ),
        "followup_sent": (
            f"Sent {d.get('step')} ({d.get('channel')}, {d.get('variant', 'standard')})"
        ),
        "followup_skipped": f"Skipped {d.get('step')}: {d.get('reason')}",
        "stage_changed": f"Stage {d.get('from')} -> {d.get('to')}",
        "lead_reopened": "Reopened from lost",
        "handoff": f"Handed to a human ({d.get('reason')})",
        "inbound_reply": "Lead replied by SMS",
        "opted_out": "Opted out (STOP)",
        "opted_in": "Opted back in (START)",
        "sequence_paused": "Follow-ups paused",
        "sequence_stopped": f"Follow-ups stopped ({d.get('reason')})",
        "sequence_resumed": f"Follow-ups resumed ({d.get('requeued_sends')} steps)",
        "crm_synced": f"Synced to CRM ({d.get('stage')})",
        "human_contact": f"Contacted by a person ({d.get('channel')})",
        "contact_revealed": "Contact details revealed",
        "lead_assigned": f"Assigned to {d.get('to') or 'nobody'}",
        "task_closed": f"Task closed ({d.get('reason')})",
        "lead_anonymized": "Personal data removed (retention)",
    }
    return texts.get(kind, kind.replace("_", " "))


def recent_activity(session: Session, limit: int = 15) -> list[ActivityRow]:
    rows = session.execute(
        select(Activity, Lead.name)
        .join(Lead, Lead.id == Activity.lead_id)
        .order_by(Activity.created_at.desc(), Activity.id.desc())
        .limit(limit)
    )
    return [
        ActivityRow(
            id=a.id,
            lead_id=a.lead_id,
            lead_name=name,
            kind=a.kind,
            actor=a.actor,
            created_at=a.created_at,
            summary=activity_summary(a.kind, a.detail),
        )
        for a, name in rows
    ]


# --------------------------------------------------------------------------- lead list


@dataclass(frozen=True)
class LeadFilter:
    stage: str | None = None
    source: str | None = None
    min_score: int | None = None
    max_score: int | None = None
    assignee_id: int | None = None
    unassigned: bool = False


def list_leads(
    session: Session, filters: LeadFilter, *, limit: int = 50, offset: int = 0
) -> tuple[list[Lead], int]:
    conds: list[ColumnElement[bool]] = []
    if filters.stage:
        conds.append(Lead.stage == filters.stage)
    if filters.source:
        conds.append(Lead.source == filters.source)
    if filters.min_score is not None:
        conds.append(Lead.score >= filters.min_score)
    if filters.max_score is not None:
        conds.append(Lead.score <= filters.max_score)
    if filters.assignee_id is not None:
        conds.append(Lead.assignee_id == filters.assignee_id)
    if filters.unassigned:
        conds.append(Lead.assignee_id.is_(None))
    total = int(session.scalar(select(func.count(Lead.id)).where(*conds)) or 0)
    leads = list(
        session.scalars(
            select(Lead)
            .where(*conds)
            .order_by(Lead.created_at.desc(), Lead.id)
            .limit(limit)
            .offset(offset)
        )
    )
    return leads, total


def known_sources(session: Session) -> list[str]:
    return list(session.scalars(select(Lead.source).distinct().order_by(Lead.source)))
