from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import init_db, make_engine, make_session_factory
from app.models import Activity, Job, Lead, LeadStage
from app.services.pipeline import (
    ALLOWED_TRANSITIONS,
    TransitionError,
    can_move,
    log_human_contact,
    move_stage,
)

T0 = datetime(2026, 3, 2, 15, tzinfo=UTC)
S = LeadStage


@pytest.fixture
def session() -> Iterator[Session]:
    engine = make_engine("sqlite://")
    init_db(engine)
    with make_session_factory(engine)() as s:
        yield s
    engine.dispose()


def _lead(session: Session, stage: LeadStage = S.NEW) -> Lead:
    lead = Lead(name="Ari Cole", source="api", stage=stage.value, created_at=T0)
    session.add(lead)
    session.flush()
    return lead


def test_every_stage_has_rules_and_terminal_stages_are_final() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(LeadStage)
    assert ALLOWED_TRANSITIONS[S.WON] == frozenset()  # won is final
    assert ALLOWED_TRANSITIONS[S.LOST] == frozenset({S.QUALIFIED})  # reopen only
    for targets in ALLOWED_TRANSITIONS.values():
        assert S.NEW not in targets  # nothing goes back to new


@pytest.mark.parametrize(
    ("src", "dst", "ok"),
    [
        (S.NEW, S.QUALIFIED, True),
        (S.NEW, S.CONTACTED, True),
        (S.NEW, S.LOST, True),
        (S.NEW, S.WON, False),  # must be qualified or contacted first
        (S.QUALIFIED, S.CONTACTED, True),
        (S.QUALIFIED, S.WON, True),
        (S.CONTACTED, S.QUALIFIED, False),
        (S.CONTACTED, S.WON, True),
        (S.WON, S.LOST, False),
        (S.LOST, S.QUALIFIED, True),  # reopen (reason required)
        (S.LOST, S.CONTACTED, False),
        (S.LOST, S.WON, False),
        (S.WON, S.QUALIFIED, False),
    ],
)
def test_transition_map(src: LeadStage, dst: LeadStage, ok: bool) -> None:
    assert can_move(src, dst) is ok


def test_move_writes_activity_with_actor_and_from_to(session: Session) -> None:
    lead = _lead(session)
    activity = move_stage(session, lead, S.QUALIFIED, actor="human:dana", now=T0)
    assert lead.stage == "qualified"
    assert activity.actor == "human:dana"
    assert activity.detail == {"from": "new", "to": "qualified", "reason": None}
    assert lead.sequence_state == "active"  # qualified does not stop automation
    key = session.scalars(select(Job.idempotency_key)).one()
    assert key == f"crm_sync:{lead.id}:qualified"


@pytest.mark.parametrize(
    ("stage", "target", "actor", "reason", "code"),
    [
        (S.NEW, S.WON, "human:dana", None, "invalid_transition"),
        (S.NEW, S.NEW, "human:dana", None, "same_stage"),
        (S.NEW, S.LOST, "human:dana", None, "reason_required"),
        (S.NEW, S.LOST, "human:dana", "   ", "reason_required"),
        (S.NEW, S.QUALIFIED, "ai:claude", None, "human_only"),
        (S.NEW, S.QUALIFIED, "system", None, "human_only"),
        (S.NEW, S.QUALIFIED, "human:", None, "human_only"),
        (S.WON, S.LOST, "human:dana", "changed mind", "invalid_transition"),
    ],
)
def test_rejected_moves(
    session: Session,
    stage: LeadStage,
    target: LeadStage,
    actor: str,
    reason: str | None,
    code: str,
) -> None:
    lead = _lead(session, stage)
    with pytest.raises(TransitionError) as exc:
        move_stage(session, lead, target, actor=actor, now=T0, reason=reason)
    assert exc.value.code == code
    assert lead.stage == stage.value
    assert session.scalars(select(Activity)).first() is None


def test_lost_stores_reason_and_stops_sequence(session: Session) -> None:
    lead = _lead(session)
    move_stage(session, lead, S.LOST, actor="human:dana", now=T0, reason="went with a competitor")
    assert lead.lost_reason == "went with a competitor"
    assert lead.sequence_state == "stopped"
    kinds = [a.kind for a in session.scalars(select(Activity).order_by(Activity.id))]
    assert kinds == ["stage_changed", "sequence_stopped"]


def test_contacted_records_first_response_once(session: Session) -> None:
    lead = _lead(session)
    move_stage(session, lead, S.CONTACTED, actor="human:dana", now=T0, crm_enabled=False)
    assert lead.first_response_at == T0
    assert session.scalars(select(Job)).first() is None  # CRM disabled -> no sync job


def test_log_human_contact(session: Session) -> None:
    lead = _lead(session)
    log_human_contact(session, lead, actor="human:dana", channel="call", now=T0, note=None)
    assert lead.first_response_at == T0
    assert lead.sequence_state == "stopped"
    assert lead.stage == "new"  # logging contact does not move the stage
    with pytest.raises(TransitionError):
        log_human_contact(session, lead, actor="ai:grok", channel="call", now=T0, note=None)


def test_reopen_lost_needs_a_reason_and_is_audited(session: Session) -> None:
    lead = _lead(session)
    move_stage(session, lead, S.LOST, actor="human:dana", now=T0, reason="went quiet")
    with pytest.raises(TransitionError) as err:
        move_stage(session, lead, S.QUALIFIED, actor="human:dana", now=T0, reason="  ")
    assert err.value.code == "reason_required"
    move_stage(session, lead, S.QUALIFIED, actor="human:lee", now=T0, reason="called back")
    session.flush()
    assert lead.stage == "qualified"
    assert lead.lost_reason is None
    reopened = session.scalars(
        select(Activity).where(Activity.lead_id == lead.id, Activity.kind == "lead_reopened")
    ).one()
    assert reopened.actor == "human:lee"
    assert reopened.detail == {"reason": "called back", "previous_lost_reason": "went quiet"}
    assert lead.sequence_state == "stopped"  # automation does not restart on reopen
    # The CRM sync for "qualified" runs again (a new key per reopen), and so would "lost".
    keys = set(session.scalars(select(Job.idempotency_key).where(Job.kind == "crm_sync")))
    assert keys == {
        f"crm_sync:{lead.id}:lost",
        f"crm_sync:{lead.id}:qualified:r1",
    }
    move_stage(session, lead, S.LOST, actor="human:lee", now=T0, reason="chose a competitor")
    session.flush()
    assert f"crm_sync:{lead.id}:lost:r1" in set(
        session.scalars(select(Job.idempotency_key).where(Job.kind == "crm_sync"))
    )
