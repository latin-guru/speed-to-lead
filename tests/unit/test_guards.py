from collections.abc import Iterator
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.orm import Session

from app.db import init_db, make_engine, make_session_factory
from app.followups.guards import (
    DEFAULT_SEND_WINDOWS,
    SendWindow,
    check_frequency,
    lead_zone,
    next_send_time,
    outside_window,
    sends_paused,
    set_sends_paused,
)
from app.followups.wording import call_phrase, received_phrase
from app.models import Lead, OutboxMessage

CHI = ZoneInfo("America/Chicago")


def _utc(y: int, mo: int, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


def _local(y: int, mo: int, d: int, h: int, mi: int = 0, tz: ZoneInfo = CHI) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=tz).astimezone(UTC)


NY = ZoneInfo("America/New_York")
WINDOW = SendWindow.parse(DEFAULT_SEND_WINDOWS)  # mon-sat 09-20, sun 15-18


def _ny(y: int, mo: int, d: int, h: int, mi: int = 0) -> datetime:
    return _local(y, mo, d, h, mi, tz=NY)


# 2026-03-02 is a Monday; 2026-03-07 a Saturday; 2026-03-08 a Sunday (US DST starts 02:00).
@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (_ny(2026, 3, 2, 10), _ny(2026, 3, 2, 10)),  # weekday daytime: now
        (_ny(2026, 3, 2, 9), _ny(2026, 3, 2, 9)),  # opens exactly at 09:00
        (_ny(2026, 3, 2, 19, 59), _ny(2026, 3, 2, 19, 59)),  # last allowed minute
        (_ny(2026, 3, 2, 20), _ny(2026, 3, 3, 9)),  # closes at 20:00 -> tomorrow 09:00
        (_ny(2026, 3, 2, 23, 30), _ny(2026, 3, 3, 9)),
        (_ny(2026, 3, 3, 2), _ny(2026, 3, 3, 9)),  # small hours -> today 09:00
        (_ny(2026, 3, 7, 12), _ny(2026, 3, 7, 12)),  # Saturday is a full day
        (_ny(2026, 3, 7, 21), _ny(2026, 3, 8, 15)),  # Saturday night -> Sunday 15:00
        (_ny(2026, 3, 8, 10), _ny(2026, 3, 8, 15)),  # Sunday morning -> Sunday 15:00
        (_ny(2026, 3, 8, 16), _ny(2026, 3, 8, 16)),  # inside Sunday's short window
        (_ny(2026, 3, 8, 18), _ny(2026, 3, 9, 9)),  # Sunday 18:00 -> Monday 09:00
    ],
)
def test_send_window(now: datetime, expected: datetime) -> None:
    assert next_send_time(now, NY, WINDOW) == expected
    assert outside_window(now, NY, WINDOW) is (expected != now)


def test_send_window_across_dst_change() -> None:
    # 21:00 Saturday 2026-03-07 is EST (UTC-5); Sunday 15:00 is EDT (UTC-4): 19:00 UTC.
    opens = next_send_time(_ny(2026, 3, 7, 21), NY, WINDOW)
    assert opens == _utc(2026, 3, 8, 19)
    assert opens.astimezone(NY).hour == 15


def test_send_window_in_the_leads_zone() -> None:
    la = ZoneInfo("America/Los_Angeles")
    # 07:00 Monday in Los Angeles is 10:00 in New York, but the lead's clock is what counts.
    assert next_send_time(_local(2026, 3, 2, 7, tz=la), la, WINDOW) == _local(2026, 3, 2, 9, tz=la)


def test_send_window_parsing() -> None:
    w = SendWindow.parse("mon,wed 08:30-12:00; fri-sat 10:00-11:00; mon 09:00-10:00")
    assert w.hours[0] == (time(9), time(10))  # later rule wins
    assert w.hours[2] == (time(8, 30), time(12))
    assert w.hours[1] is None
    assert w.hours[6] is None
    assert w.hours[4] == w.hours[5] == (time(10), time(11))
    # A window that is only open on Wednesday still finds the next opening.
    only_wed = SendWindow.parse("wed 09:00-10:00")
    assert next_send_time(_ny(2026, 3, 5, 12), NY, only_wed) == _ny(2026, 3, 11, 9)


@pytest.mark.parametrize(
    "spec",
    [
        "",
        " ; ",
        "mon 9-17",
        "mon 09:00-09:00",
        "mon 17:00-09:00",
        "funday 09:00-10:00",
        "sat-mon 09:00-10:00",
        "mon09:00-10:00",
        "mon 09:00",
        "mon-xyz 09:00-10:00",
    ],
)
def test_send_window_rejects_bad_specs(spec: str) -> None:
    with pytest.raises(ValueError, match=r"send window|day"):
        SendWindow.parse(spec)


def test_window_with_no_open_day_cannot_be_built() -> None:
    with pytest.raises(ValueError, match="no open hours"):
        next_send_time(_ny(2026, 3, 2, 10), NY, SendWindow((None,) * 7))


@pytest.mark.parametrize(
    ("created", "sent", "received", "call"),
    [
        (_ny(2026, 3, 2, 22, 30), _ny(2026, 3, 3, 9), "last night", "this morning"),
        (_ny(2026, 3, 3, 2), _ny(2026, 3, 3, 9), "last night", "this morning"),
        (_ny(2026, 3, 3, 7), _ny(2026, 3, 3, 9), "earlier today", "this morning"),
        (_ny(2026, 3, 7, 21), _ny(2026, 3, 8, 15), "last night", "this afternoon"),
        (_ny(2026, 3, 8, 10), _ny(2026, 3, 8, 15), "earlier today", "this afternoon"),
        (_ny(2026, 3, 2, 12), _ny(2026, 3, 3, 18), "yesterday", "this evening"),
        (_ny(2026, 3, 6, 12), _ny(2026, 3, 9, 9), "on Friday", "this morning"),
    ],
)
def test_after_hours_phrases(created: datetime, sent: datetime, received: str, call: str) -> None:
    assert received_phrase(created, sent, NY) == received
    assert call_phrase(sent, NY) == call


def test_lead_zone_falls_back_to_business_tz() -> None:
    assert str(lead_zone("America/Denver", "America/Chicago")) == "America/Denver"
    assert str(lead_zone(None, "America/Chicago")) == "America/Chicago"
    assert str(lead_zone("Not/AZone", "America/Chicago")) == "America/Chicago"


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


def _sent(session: Session, lead: Lead, at: datetime, n: int, transactional: bool = False) -> None:
    session.add(
        OutboxMessage(
            lead_id=lead.id,
            step=f"s{n}",
            channel="sms",
            to_address="x",
            body="b",
            sender="MOCK-sms",
            idempotency_key=f"{lead.id}:s{n}:{at.isoformat()}",
            transactional=transactional,
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


def test_instant_ack_pair_is_one_touch(session: Session) -> None:
    lead = _lead(session, email="sam@example.com")
    _sent(session, lead, T0, 1, transactional=True)  # ack SMS
    assert _check(session, lead, T0, transactional=True).allowed  # type: ignore[attr-defined]
    _sent(session, lead, T0, 2, transactional=True)  # ack email: same touch
    _sent(session, lead, T0 + timedelta(days=1), 3)
    _sent(session, lead, T0 + timedelta(days=2), 4)
    # ack pair + 2 reminders = 3 touches: the cap (3) is reached.
    d = _check(session, lead, T0 + timedelta(days=3))
    assert (d.allowed, d.reason, d.not_before) == (False, "weekly_cap", T0 + timedelta(days=7))  # type: ignore[attr-defined]
    # A second lead for the same person gets its own ack touch, which the cap also counts.
    dup = _lead(session, phone="+12025550199", email="sam@example.com")
    d = _check(session, dup, T0 + timedelta(days=3), transactional=True)
    assert (d.allowed, d.reason) == (False, "weekly_cap")  # type: ignore[attr-defined]
