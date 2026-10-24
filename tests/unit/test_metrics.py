"""Metrics SQL on small, fixed data sets (hand-computed expectations)."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.orm import Session

from app.db import init_db, make_engine, make_session_factory
from app.models import Activity, Job, Lead, Task
from app.services import metrics as m

T0 = datetime(2026, 3, 2, 15, tzinfo=UTC)
WEEK = m.Period(T0 - timedelta(days=7), T0)
NY = ZoneInfo("America/New_York")


@pytest.fixture
def session() -> Iterator[Session]:
    engine = make_engine("sqlite://")
    init_db(engine)
    with make_session_factory(engine)() as s:
        yield s
    engine.dispose()


def _lead(
    session: Session,
    *,
    ago: timedelta,
    response: float | None = None,
    stage: str = "new",
    source: str = "web_form",
    score: int | None = None,
    moves: tuple[str, ...] = (),
) -> Lead:
    created = T0 - ago
    lead = Lead(
        name="Pat Example",
        source=source,
        stage=stage,
        score=score,
        created_at=created,
        first_response_at=None if response is None else created + timedelta(seconds=response),
    )
    session.add(lead)
    session.flush()
    prev = "new"
    for to in moves:
        session.add(
            Activity(
                lead_id=lead.id,
                kind="stage_changed",
                actor="human:x",
                detail={"from": prev, "to": to},
            )
        )
        prev = to
    session.flush()
    return lead


def test_median_response_odd_and_even(session: Session) -> None:
    assert m.median_response_seconds(session, WEEK) is None
    for secs in (30, 5, 600):
        _lead(session, ago=timedelta(days=1), response=secs)
    _lead(session, ago=timedelta(days=1))  # no response yet: not counted
    _lead(session, ago=timedelta(days=9), response=1)  # outside the period
    assert m.median_response_seconds(session, WEEK) == 30.0
    _lead(session, ago=timedelta(days=2), response=90)
    assert m.median_response_seconds(session, WEEK) == 60.0  # (30 + 90) / 2


def test_median_handles_sub_second_and_long_gaps(session: Session) -> None:
    _lead(session, ago=timedelta(days=3), response=0.5)
    _lead(session, ago=timedelta(days=3), response=13 * 3600)  # after-hours lead
    assert m.median_response_seconds(session, WEEK) == pytest.approx(23400.25, abs=0.01)


def test_funnel_counts_furthest_stage_and_conversion(session: Session) -> None:
    _lead(session, ago=timedelta(days=1))  # new
    _lead(session, ago=timedelta(days=1))  # new
    _lead(session, ago=timedelta(days=1), stage="qualified", moves=("qualified",))
    _lead(session, ago=timedelta(days=1), stage="contacted", moves=("contacted",))  # skipped q
    _lead(session, ago=timedelta(days=1), stage="won", moves=("qualified", "contacted", "won"))
    # Got to contacted, then lost: still counts as reaching qualified and contacted.
    _lead(session, ago=timedelta(days=1), stage="lost", moves=("contacted", "lost"))
    _lead(session, ago=timedelta(days=1), stage="lost", moves=("lost",))  # lost from new
    _lead(session, ago=timedelta(days=20), stage="won", moves=("qualified", "won"))  # outside

    steps = m.funnel(session, WEEK)
    assert [(s.stage, s.reached) for s in steps] == [
        ("new", 7),
        ("qualified", 4),
        ("contacted", 3),
        ("won", 1),
    ]
    assert [s.conversion for s in steps] == [None, 0.5714, 0.75, 0.3333]
    assert m.win_rate(session, WEEK) == 0.1429  # 1 of 7
    counts = m.stage_counts(session, WEEK)
    assert counts == {"new": 2, "qualified": 1, "contacted": 1, "won": 1, "lost": 2}


def test_reopened_lead_counts_once(session: Session) -> None:
    _lead(session, ago=timedelta(days=1), stage="qualified", moves=("lost", "qualified"))
    assert [s.reached for s in m.funnel(session, WEEK)] == [1, 1, 0, 0]


def test_empty_period(session: Session) -> None:
    assert [s.reached for s in m.funnel(session, WEEK)] == [0, 0, 0, 0]
    assert [s.conversion for s in m.funnel(session, WEEK)] == [None, None, None, None]
    assert m.win_rate(session, WEEK) is None
    assert m.average_score(session, WEEK) is None
    assert m.lead_count(session, WEEK) == 0
    assert m.leads_by_source(session, WEEK) == []


def test_sources_scores_and_histogram(session: Session) -> None:
    for source, score in [
        ("meta_ads", 95),
        ("meta_ads", 100),
        ("web_form", 0),
        ("web_form", 9),
        ("web_form", 10),
        ("referral", 55),
    ]:
        _lead(session, ago=timedelta(days=2), source=source, score=score)
    _lead(session, ago=timedelta(days=2), source="google_ads")  # not scored yet
    assert m.leads_by_source(session, WEEK) == [
        ("web_form", 3),
        ("meta_ads", 2),
        ("google_ads", 1),
        ("referral", 1),
    ]
    assert m.average_score(session, WEEK) == 44.8  # 269 / 6
    hist = dict(m.score_histogram(session, WEEK))
    assert hist["0-9"] == 2
    assert hist["10-19"] == 1
    assert hist["50-59"] == 1
    assert hist["90-100"] == 2  # 95 and 100
    assert sum(hist.values()) == 6
    assert len(hist) == 10
    assert m.lead_count(session, WEEK) == 7


def test_response_trend_by_local_day(session: Session) -> None:
    # T0 is 10:00 in New York. 1 day ago and 1 day + 1h ago are the same local day.
    _lead(session, ago=timedelta(days=1), response=10)
    _lead(session, ago=timedelta(days=1, hours=1), response=30)
    _lead(session, ago=timedelta(days=1, hours=2), response=50)
    _lead(session, ago=timedelta(days=3), response=7)
    trend = m.response_trend(session, WEEK, NY)
    assert len(trend) == 8  # 7 days, starting and ending part-way through a local day
    by_day = {d.isoformat(): (median, n) for d, median, n in trend}
    assert by_day["2026-03-01"] == (30.0, 3)
    assert by_day["2026-02-27"] == (7.0, 1)
    assert by_day["2026-02-28"] == (None, 0)


def test_counts_tasks_and_dead_jobs(session: Session) -> None:
    lead = _lead(session, ago=timedelta(days=1))
    session.add_all(
        [
            Task(lead_id=lead.id, reason="lead_replied"),
            Task(lead_id=lead.id, reason="ai_human_review", status="done"),
            Job(kind="crm_sync", payload={}, idempotency_key="a", status="dead", run_at=T0),
            Job(kind="crm_sync", payload={}, idempotency_key="b", status="done", run_at=T0),
        ]
    )
    session.flush()
    assert m.open_task_count(session) == 1
    assert m.dead_job_count(session) == 1


def test_recent_activity_summaries_have_no_free_text(session: Session) -> None:
    lead = _lead(session, ago=timedelta(days=1))
    session.add_all(
        [
            Activity(
                lead_id=lead.id,
                kind="inbound_reply",
                actor="lead",
                detail={"body": "call 202-555-0111"},
            ),
            Activity(
                lead_id=lead.id,
                kind="ai_scored",
                actor="ai:mock",
                detail={"score": 70, "scored_by": "mock", "next_step": "call_now"},
            ),
            Activity(lead_id=lead.id, kind="something_new", actor="system", detail={}),
        ]
    )
    session.flush()
    rows = m.recent_activity(session, limit=10)
    assert [r.summary for r in rows] == [
        "something new",
        "Scored 70 by mock, suggests call_now",
        "Lead replied by SMS",
    ]
    assert all("555" not in r.summary for r in rows)
    assert rows[0].lead_name == "Pat Example"


def test_lead_list_filters(session: Session) -> None:
    _lead(session, ago=timedelta(days=1), source="meta_ads", score=80, stage="qualified")
    _lead(session, ago=timedelta(days=2), source="meta_ads", score=40)
    _lead(session, ago=timedelta(days=3), source="referral", score=90)
    leads, total = m.list_leads(session, m.LeadFilter(source="meta_ads"))
    assert total == 2
    assert [lead.score for lead in leads] == [80, 40]  # newest first
    _, total = m.list_leads(session, m.LeadFilter(min_score=50, max_score=85))
    assert total == 1
    _, total = m.list_leads(session, m.LeadFilter(stage="qualified"))
    assert total == 1
    page, total = m.list_leads(session, m.LeadFilter(), limit=1, offset=1)
    assert (total, [lead.score for lead in page]) == (3, [40])
    assert m.known_sources(session) == ["meta_ads", "referral"]


def test_period_helper() -> None:
    p = m.Period.last_days(T0, 30)
    assert (p.end - p.start).days == 30
