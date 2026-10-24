from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.orm import Session

from app.db import init_db, make_engine, make_session_factory
from app.followups.guards import (
    check_frequency,
    lead_zone,
    next_send_time,
    sends_paused,
    set_sends_paused,
)
from app.models import Lead, OutboxMessage

CHI = ZoneInfo("America/Chicago")


def _utc(y: int, mo: int, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


def _local(y: int, mo: int, d: int, h: int, mi: int = 0, tz: ZoneInfo = CHI) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=tz).astimezone(UTC)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (_local(2026, 3, 2, 10), _local(2026, 3, 2, 10)),  # daytime: now
        (_local(2026, 3, 2, 20, 59), _local(2026, 3, 2, 20, 59)),  # last allowed minute
        (_local(2026, 3, 2, 21), _local(2026, 3, 3, 8)),  # evening -> tomorrow 08:00
        (_local(2026, 3, 2, 23, 30), _local(2026, 3, 3, 8)),
        (_local(2026, 3, 3, 2), _local(2026, 3, 3, 8)),  # small hours -> today 08:00
        (_local(2026, 3, 3, 8), _local(2026, 3, 3, 8)),  # quiet ends exactly at 08:00
    ],
)
def test_quiet_hours_overnight(now: datetime, expected: datetime) -> None:
    assert next_send_time(now, CHI, quiet_start=21, quiet_end=8) == expected


def test_quiet_hours_across_dst_change() -> None:
    # US DST starts 2026-03-08 at 02:00 local. 22:00 on the 7th is CST (UTC-6) and 08:00 on
    # the 8th is CDT (UTC-5): quiet hours must end at 08:00 *local*, i.e. 13:00 UTC, not 14:00.
    opens = next_send_time(_local(2026, 3, 7, 22), CHI, quiet_start=21, quiet_end=8)
    assert opens == _utc(2026, 3, 8, 13)  # 08:00 CDT
    assert opens.astimezone(CHI).hour == 8


def test_quiet_hours_window_within_a_day() -> None:
    lunch = next_send_time(_local(2026, 3, 2, 12, 30), CHI, quiet_start=12, quiet_end=13)
    assert lunch == _local(2026, 3, 2, 13)
    assert next_send_time(_local(2026, 3, 2, 14), CHI, quiet_start=12, quiet_end=13) == _local(
        2026, 3, 2, 14
    )


def test_lead_zone_falls_back_to_business_tz() -> None:
    assert lead_zone("America/Denver", "America/Chicago").key == "America/Denver"
    assert lead_zone(None, "America/Chicago").key == "America/Chicago"
    assert lead_zone("Not/AZone", "America/Chicago").key == "America/Chicago"


@pytest.fixture
def session() -> Iterator[Session]:
    engine = make_engine("sqlite://")
    init_db(engine)
    with make_session_factory(engine)() as s:
        yield s
    engine.dispose()


T0 = _utc(2026, 3, 2, 15)


def _lead(session: Session, phone: str = "+12025550111", email: str | None = None) -> Lead:
    lead = Lead(name="Sam Roe", source="api", phone=phone, email=email, created_at=T0)
    session.add(lead)
    session.flush()
    return lead


def _sent(session: Session, lead: Lead, at: datetime, n: int) -> None:
    session.add(
        OutboxMessage(
            lead_id=lead.id,
            step=f"s{n}",
            channel="sms",
            to_address="x",
            body="b",
            sender="MOCK-sms",
            idempotency_key=f"{lead.id}:s{n}:{at.isoformat()}",
            sent_at=at,
        )
    )
    session.flush()


def _check(session: Session, lead: Lead, now: datetime, transactional: bool = False) -> object:
    return check_frequency(
        session,
        lead,
        now=now,
        max_per_week=3,
        min_gap=timedelta(hours=12),
        transactional=transactional,
    )


def test_frequency_allows_first_touch(session: Session) -> None:
    assert _check(session, _lead(session), T0).allowed  # type: ignore[attr-defined]


def test_min_gap_defers_marketing_but_not_transactional(session: Session) -> None:
    lead = _lead(session)
    _sent(session, lead, T0, 1)
    d = _check(session, lead, T0 + timedelta(hours=1))
    assert (d.allowed, d.reason, d.not_before) == (False, "min_gap", T0 + timedelta(hours=12))  # type: ignore[attr-defined]
    assert _check(session, lead, T0 + timedelta(hours=1), transactional=True).allowed  # type: ignore[attr-defined]
    assert _check(session, lead, T0 + timedelta(hours=12)).allowed  # type: ignore[attr-defined]


def test_weekly_cap_counts_every_lead_for_the_same_person(session: Session) -> None:
    first = _lead(session, email="sam@example.com")
    dup = _lead(session, phone="+12025550199", email="sam@example.com")  # same person, new form
    _sent(session, first, T0, 1)
    _sent(session, first, T0 + timedelta(days=1), 2)
    _sent(session, dup, T0 + timedelta(days=2), 3)
    d = _check(session, dup, T0 + timedelta(days=3), transactional=True)
    assert (d.allowed, d.reason) == (False, "weekly_cap")  # type: ignore[attr-defined]
    assert d.not_before == T0 + timedelta(days=7)  # type: ignore[attr-defined]  # oldest ages out
    other = _lead(session, phone="+12025550188")
    assert _check(session, other, T0 + timedelta(days=3)).allowed  # type: ignore[attr-defined]
    assert _check(session, dup, T0 + timedelta(days=7)).allowed  # type: ignore[attr-defined]


def test_kill_switch_env_and_db(session: Session) -> None:
    assert not sends_paused(session, env_paused=False)
    assert sends_paused(session, env_paused=True)
    set_sends_paused(session, True, actor="human:ops")
    assert sends_paused(session, env_paused=False)
    flag = set_sends_paused(session, False, actor="human:ops")
    assert flag.value == "false"
    assert not sends_paused(session, env_paused=False)
