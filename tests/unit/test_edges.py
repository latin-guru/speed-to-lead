"""Edge cases: config validation, UTC column type, timezone input, scoring handoffs."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.schemas import NextStep, ScoreResult
from app.ai.service import ScoreOutcome
from app.db import UTCDateTime, init_db, make_engine, make_session_factory, utc
from app.followups.engine import is_stop_keyword, pause_sequence, stop_sequence
from app.models import Lead, Task
from app.schemas import LeadIn
from app.services.scoring import score_lead
from tests.conftest import make_settings, valid_lead

T0 = datetime(2026, 3, 2, 15, tzinfo=UTC)


def test_settings_reject_bad_timezone_and_empty_quiet_window() -> None:
    with pytest.raises(ValidationError, match="BUSINESS_TIMEZONE"):
        make_settings(business_timezone="Mars/Olympus")
    with pytest.raises(ValidationError, match="SEND_WINDOWS"):
        make_settings(send_windows="mon 09:00-09:00")


def test_utc_type_rejects_naive_and_normalizes() -> None:
    t = UTCDateTime()
    with pytest.raises(ValueError, match="naive"):
        t.process_bind_param(datetime(2026, 1, 1), None)  # type: ignore[arg-type]
    plus2 = datetime(2026, 1, 1, 12, tzinfo=timezone(timedelta(hours=2)))
    assert t.process_bind_param(plus2, None) == datetime(2026, 1, 1, 10, tzinfo=UTC)  # type: ignore[arg-type]
    assert t.process_result_value(plus2, None).tzinfo is UTC  # type: ignore[arg-type,union-attr]
    assert t.process_result_value(None, None) is None  # type: ignore[arg-type]
    assert utc(datetime(2026, 1, 1)) == datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    ("value", "ok"),
    [("America/Denver", True), ("UTC", True), ("", True), ("EST", False), ("Not/AZone", False)],
)
def test_lead_timezone_input(value: str, ok: bool) -> None:
    if ok:
        LeadIn.model_validate(valid_lead(timezone=value))
    else:
        with pytest.raises(ValidationError):
            LeadIn.model_validate(valid_lead(timezone=value))


def test_stop_keywords() -> None:
    for body in ("STOP", " stop. ", "Unsubscribe", "quit!", "END"):
        assert is_stop_keyword(body)
    for body in ("please stop calling after 5", "stopping by later", ""):
        assert not is_stop_keyword(body)


@pytest.fixture
def session() -> Iterator[Session]:
    engine = make_engine("sqlite://")
    init_db(engine)
    with make_session_factory(engine)() as s:
        yield s
    engine.dispose()


def _lead(session: Session) -> Lead:
    lead = Lead(name="Rae Kim", source="api", phone="+12025550166", created_at=T0)
    session.add(lead)
    session.flush()
    return lead


class FakeService:
    """Bypasses the policy layer to return a low-confidence score with a confident step."""

    min_confidence = 0.5

    def __init__(self, confidence: float, step: NextStep = NextStep.CALL_NOW) -> None:
        self.result = ScoreResult(
            score=60, summary="Wants a quote.", next_step=step, confidence=confidence
        )
        self.calls = 0

    def score(self, ctx: object) -> ScoreOutcome:
        self.calls += 1
        return ScoreOutcome(self.result, "mock", "mock", 1, 1)


def test_low_confidence_hands_off_and_scoring_is_once(session: Session) -> None:
    lead = _lead(session)
    svc = FakeService(confidence=0.2)
    assert score_lead(session, lead.id, svc, now=T0) == "scored"  # type: ignore[arg-type]
    assert score_lead(session, lead.id, svc, now=T0) == "already_scored"  # type: ignore[arg-type]
    assert svc.calls == 1
    task = session.scalars(select(Task)).one()
    assert task.reason == "low_confidence"
    assert lead.sequence_state == "paused"
    assert lead.stage == "new"
    assert score_lead(session, uuid.uuid4(), svc, now=T0) == "skipped:lead_missing"  # type: ignore[arg-type]


def test_confident_score_does_not_hand_off(session: Session) -> None:
    lead = _lead(session)
    score_lead(session, lead.id, FakeService(confidence=0.9), now=T0)  # type: ignore[arg-type]
    assert session.scalars(select(Task)).first() is None
    assert lead.sequence_state == "active"


def test_pause_never_reactivates_a_stopped_sequence(session: Session) -> None:
    lead = _lead(session)
    stop_sequence(session, lead, "opted_out", now=T0)
    pause_sequence(session, lead, "handoff:lead_replied", now=T0)
    assert lead.sequence_state == "stopped"
