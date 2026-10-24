"""Dashboard metrics as JSON (``/api/metrics/*``).

Readable with either the operator bearer token or a dashboard session cookie. The dashboard
polls ``/api/metrics/summary`` every few seconds to refresh itself.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import SessionDep, dashboard_session, require_admin
from app.models import utcnow
from app.services import metrics as m

KPI_DAYS = 7
CHART_DAYS = 30


def require_metrics_access(request: Request) -> None:
    """Bearer ``ADMIN_TOKEN`` or a valid dashboard session."""
    if dashboard_session(request) is not None:
        return
    if request.headers.get("authorization"):
        require_admin(request)  # raises 401 / 503 with the usual details
        return
    raise HTTPException(
        status_code=401,
        detail="log in or send a bearer token",
        headers={"WWW-Authenticate": "Bearer"},
    )


router = APIRouter(
    prefix="/api/metrics", tags=["metrics"], dependencies=[Depends(require_metrics_access)]
)


class KpisOut(BaseModel):
    period_days: int = Field(description="KPIs cover leads created in the last N days")
    leads: int
    leads_previous: int = Field(description="leads in the N days before that")
    median_first_response_seconds: float | None
    win_rate: float | None = Field(description="New -> Won: won / all leads in the period")
    average_score: float | None
    open_tasks: int = Field(description="open handoff tasks (all time)")
    dead_jobs: int = Field(description="dead-lettered jobs (all time)")


class FunnelStepOut(BaseModel):
    stage: str
    reached: int = Field(description="leads that reached this stage or a later one")
    conversion: float | None = Field(description="share of the previous step that got here")


class FunnelOut(BaseModel):
    period_days: int
    steps: list[FunnelStepOut]
    lost: int
    stages: dict[str, int] = Field(description="current stage counts")


class LabelCount(BaseModel):
    label: str
    count: int


class TrendPoint(BaseModel):
    day: date
    median_seconds: float | None
    responded: int


class ActivityItem(BaseModel):
    id: int
    lead_id: uuid.UUID
    lead_name: str
    kind: str
    actor: str
    created_at: datetime
    summary: str


class SummaryOut(BaseModel):
    generated_at: datetime
    kpis: KpisOut
    funnel: FunnelOut
    sources: list[LabelCount]
    scores: list[LabelCount]
    response_times: list[TrendPoint]
    activity: list[ActivityItem]


def _zone(request: Request) -> ZoneInfo:
    return ZoneInfo(request.app.state.settings.business_timezone)


def build_kpis(session: Session, now: datetime) -> KpisOut:
    period = m.Period.last_days(now, KPI_DAYS)
    previous = m.Period(period.start - (period.end - period.start), period.start)
    return KpisOut(
        period_days=KPI_DAYS,
        leads=m.lead_count(session, period),
        leads_previous=m.lead_count(session, previous),
        median_first_response_seconds=m.median_response_seconds(session, period),
        win_rate=m.win_rate(session, m.Period.last_days(now, CHART_DAYS)),
        average_score=m.average_score(session, period),
        open_tasks=m.open_task_count(session),
        dead_jobs=m.dead_job_count(session),
    )


def build_funnel(session: Session, now: datetime) -> FunnelOut:
    period = m.Period.last_days(now, CHART_DAYS)
    stages = m.stage_counts(session, period)
    return FunnelOut(
        period_days=CHART_DAYS,
        steps=[FunnelStepOut(**vars(s)) for s in m.funnel(session, period)],
        lost=stages["lost"],
        stages=stages,
    )


def build_summary(session: Session, now: datetime, zone: ZoneInfo) -> SummaryOut:
    period = m.Period.last_days(now, CHART_DAYS)
    return SummaryOut(
        generated_at=now,
        kpis=build_kpis(session, now),
        funnel=build_funnel(session, now),
        sources=[LabelCount(label=s, count=n) for s, n in m.leads_by_source(session, period)],
        scores=[LabelCount(label=b, count=n) for b, n in m.score_histogram(session, period)],
        response_times=[
            TrendPoint(day=d, median_seconds=v, responded=n)
            for d, v, n in m.response_trend(session, m.Period.last_days(now, 14), zone)
        ],
        activity=[ActivityItem(**vars(a)) for a in m.recent_activity(session, 12)],
    )


@router.get("/summary", response_model=SummaryOut)
def summary(request: Request, session: SessionDep) -> SummaryOut:
    """Everything the dashboard shows, in one call (it polls this)."""
    return build_summary(session, utcnow(), _zone(request))


@router.get("/kpis", response_model=KpisOut)
def kpis(session: SessionDep) -> KpisOut:
    return build_kpis(session, utcnow())


@router.get("/funnel", response_model=FunnelOut)
def funnel(session: SessionDep) -> FunnelOut:
    """Leads created in the last 30 days, by the furthest stage they reached."""
    return build_funnel(session, utcnow())


@router.get("/sources", response_model=list[LabelCount])
def sources(
    session: SessionDep, days: int = Query(default=CHART_DAYS, ge=1, le=365)
) -> list[LabelCount]:
    period = m.Period.last_days(utcnow(), days)
    return [LabelCount(label=s, count=n) for s, n in m.leads_by_source(session, period)]


@router.get("/scores", response_model=list[LabelCount])
def scores(
    session: SessionDep, days: int = Query(default=CHART_DAYS, ge=1, le=365)
) -> list[LabelCount]:
    """AI score distribution in 10-point buckets."""
    period = m.Period.last_days(utcnow(), days)
    return [LabelCount(label=b, count=n) for b, n in m.score_histogram(session, period)]


@router.get("/response-times", response_model=list[TrendPoint])
def response_times(
    request: Request, session: SessionDep, days: int = Query(default=14, ge=1, le=90)
) -> list[TrendPoint]:
    """Median first-response time per day (business timezone)."""
    trend = m.response_trend(session, m.Period.last_days(utcnow(), days), _zone(request))
    return [TrendPoint(day=d, median_seconds=v, responded=n) for d, v, n in trend]


@router.get("/activity", response_model=list[ActivityItem])
def activity(
    session: SessionDep, limit: int = Query(default=20, ge=1, le=100)
) -> list[ActivityItem]:
    """Most recent timeline events across all leads (no free text, no contact details)."""
    return [ActivityItem(**vars(a)) for a in m.recent_activity(session, limit)]
