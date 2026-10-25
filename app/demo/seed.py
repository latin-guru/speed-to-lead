"""Reset a demo database and fill it with ~30 days of fake, realistic history.

Everything here is invented: Faker names (fixed seed), ``555-01xx`` phone numbers and
``example.com`` emails, for the fictional Copperline Roofing & Solar. The history is built
from the *real* pieces: the default follow-up sequence and its templates, the sending
window, the after-hours wording and the built-in rule-based scorer. So the dashboard shows what the
system would have produced, not made-up numbers.

Determinism: with the same ``seed`` and ``count`` the same leads get the same stages,
sources, scores, handoffs and opt-outs, whatever the current time is. Only timestamps move
with ``now`` (they are relative to it).
"""

from __future__ import annotations

import random
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from alembic import command
from alembic.config import Config
from faker import Faker
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.ai.rules import MockScorer
from app.db import ROOT, Base
from app.followups.guards import SendWindow, next_send_time, outside_window
from app.followups.sequence import Channel, Sequence, SequenceStep, load_sequence, render
from app.followups.wording import call_phrase, received_phrase
from app.jobs import kinds
from app.models import (
    Activity,
    Job,
    JobStatus,
    Lead,
    LeadStage,
    OutboxMessage,
    SequenceState,
    Task,
    TaskStatus,
)
from app.services.handoff import AI_HUMAN_REVIEW, LEAD_REPLIED
from app.services.scoring import lead_context

SOURCES = (("web_form", 35), ("meta_ads", 30), ("google_ads", 20), ("referral", 15))
# Area codes for the fake numbers (all 555-01xx, which is reserved for fiction).
AREA_CODES = ("202", "206", "303", "312", "404", "412", "503", "512", "602", "615", "617",
              "702", "704", "713", "720", "802", "813", "919")  # fmt: skip
# Demo first names come from this fixed list (and last names from Faker), so every person in the
# demo is clearly made up and the data stays the same from run to run.
FIRST_NAMES = (
    "Avery", "Jordan", "Taylor", "Morgan", "Riley", "Casey", "Jamie", "Quinn", "Drew", "Reese",
    "Parker", "Rowan", "Skyler", "Emerson", "Hayden", "Logan", "Harper", "Elliot", "Finley",
    "Sawyer",
    "Dana", "Blake", "Cameron", "Jesse", "Kendall", "Marley", "Peyton", "Remy", "Sage", "Tatum",
    "Alex", "Robin", "Shawn", "Terry", "Lee", "Noel", "Kerry", "Jules", "Arden", "Bailey",
    "Laura", "Daniel", "Priya", "Marcus", "Sofia", "Andre", "Grace", "Omar", "Elena", "Tyler",
    "Nora", "Victor", "Hannah", "Diego", "Chloe", "Samuel", "Leah", "Mateo", "Zoe", "Caleb",
)  # fmt: skip
MESSAGES = (
    "Looking for a quote to replace our {n}-year-old asphalt roof before winter.",
    "Roof leak over the kitchen after the storm last night, need someone asap.",
    "Interested in solar panels for a {sqft} sq ft house. What would an estimate look like?",
    "We'd like solar plus a battery backup. Can someone come out for an inspection?",
    "Hail took a few shingles off the north side. Can you take a look?",
    "Gutters overflow every time it rains and some flashing came loose.",
    "Just curious about pricing for a standing-seam metal roof.",
    "Need a roof inspection before we list the house next month.",
    "Our skylight drips when it rains hard. Repair or replace?",
    "Thinking about solar next spring, just gathering information for now.",
    "Roof is {n} years old and we see daylight in the attic. Estimate please.",
    "Price?",
    "Call me",
)
SPAM = "We sell SEO backlinks and guest post packages for contractors."
REPLIES = (
    "Thursday after 3 works for me.",
    "Can you call after 5? I'm at work until then.",
    "Is the estimate free?",
    "My partner wants to be there, can we do Saturday?",
    "How long does a full replacement take?",
)
LOST_REASONS = (
    "chose another contractor",
    "over budget",
    "no response after several attempts",
    "outside service area",
    "decided to wait until next year",
)
OPERATORS = ("dana", "lee", "sam")


@dataclass
class SeedReport:
    leads: int = 0
    stages: dict[str, int] = field(default_factory=dict)
    sources: dict[str, int] = field(default_factory=dict)
    open_tasks: int = 0
    opted_out: int = 0
    dead_jobs: int = 0
    outbox: int = 0


# --------------------------------------------------------------------------- reset


def reset_database(engine: Engine) -> None:
    """Drop everything and rebuild the schema with the real Alembic migrations."""
    from app import models  # noqa: F401  (register tables)

    with engine.begin() as conn:
        Base.metadata.drop_all(conn)
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
    cfg = Config(str(Path(ROOT) / "alembic.ini"))
    cfg.attributes["configure_logger"] = False
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")


# --------------------------------------------------------------------------- fake people


def ascii_lower(value: str) -> str:
    return unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()


def email_for(first: str, last: str, suffix: str) -> str:
    local = re.sub(r"[^a-z0-9.]+", "", f"{ascii_lower(first)}.{ascii_lower(last)}.{suffix}")
    return f"{local}@example.com"


def _person(fake: Faker) -> tuple[str, str]:
    return str(fake.random_element(FIRST_NAMES)), fake.last_name()


def _weighted(rng: random.Random, options: tuple[tuple[str, int], ...]) -> str:
    return rng.choices([o for o, _ in options], weights=[w for _, w in options])[0]


def _stage_for(rng: random.Random, age_days: int) -> str:
    """Recent leads are mostly still open; older ones have mostly been decided."""
    table: tuple[tuple[str, int], ...]
    if age_days < 2:
        table = (("new", 55), ("qualified", 25), ("contacted", 20))
    elif age_days < 7:
        table = (("new", 15), ("qualified", 20), ("contacted", 30), ("won", 15), ("lost", 20))
    else:
        table = (("new", 6), ("qualified", 10), ("contacted", 20), ("won", 26), ("lost", 38))
    return _weighted(rng, table)


@dataclass(frozen=True)
class _Plan:
    """Every random choice for one lead, drawn up front in a fixed order (determinism)."""

    first: str
    last: str
    source: str
    age_days: int
    hour: int
    minute: int
    has_phone: bool
    has_email: bool
    area: str
    line: int
    message: str
    stage: str
    path_via_qualified: bool
    latency_s: float
    human_after_min: float
    decide_after_h: float
    lost_reason: str
    replied: bool
    reply: str
    opted_out: bool
    operator: str


def _plan(rng: random.Random, fake: Faker) -> _Plan:
    first, last = _person(fake)
    source = _weighted(rng, SOURCES)
    age_days = min(29, int((rng.random() ** 1.6) * 30))  # more leads recently
    night = rng.random() < 0.18
    hour = rng.choice((21, 22, 23, 0, 1, 5, 6, 7)) if night else rng.randint(8, 20)
    minute = rng.randint(0, 59)
    has_phone = rng.random() < 0.9
    has_email = (not has_phone) or rng.random() < 0.85
    area, line = rng.choice(AREA_CODES), rng.randint(0, 99)
    template = SPAM if rng.random() < 0.03 else rng.choice(MESSAGES)
    message = template.format(n=rng.randint(12, 30), sqft=rng.choice((1400, 1800, 2200, 2600)))
    return _Plan(
        first=first,
        last=last,
        source=source,
        age_days=age_days,
        hour=hour,
        minute=minute,
        has_phone=has_phone,
        has_email=has_email,
        area=area,
        line=line,
        message=message,
        stage=_stage_for(rng, age_days),
        path_via_qualified=rng.random() < 0.6,
        latency_s=rng.uniform(1.5, 25.0),
        human_after_min=rng.uniform(4, 180),
        decide_after_h=rng.uniform(20, 120),
        lost_reason=rng.choice(LOST_REASONS),
        replied=rng.random() < 0.14,
        reply=rng.choice(REPLIES),
        opted_out=rng.random() < 0.06,
        operator=rng.choice(OPERATORS),
    )


# --------------------------------------------------------------------------- building


@dataclass
class _Builder:
    session: Session
    sequence: Sequence
    window: SendWindow
    zone: ZoneInfo
    company: str
    callback_minutes: int
    now: datetime
    report: SeedReport
    _job_seq: int = 0

    def activity(
        self, lead: Lead, kind: str, actor: str, at: datetime, detail: dict[str, Any]
    ) -> None:
        self.session.add(
            Activity(lead_id=lead.id, kind=kind, actor=actor, detail=detail, created_at=at)
        )

    def job(
        self,
        lead: Lead,
        kind: str,
        key: str,
        payload: dict[str, Any],
        *,
        run_at: datetime,
        status: JobStatus,
        attempts: int = 1,
        error: str | None = None,
    ) -> None:
        self.session.add(
            Job(
                kind=kind,
                payload=payload,
                run_at=run_at,
                attempts=attempts,
                max_attempts=5,
                status=status.value,
                idempotency_key=key,
                last_error=error,
                lead_id=lead.id,
                created_at=lead.created_at,
                updated_at=min(run_at, self.now)
                if status is not JobStatus.QUEUED
                else lead.created_at,
            )
        )

    def send(self, lead: Lead, step: SequenceStep, at: datetime) -> None:
        after_hours = step.after_hours_body is not None and outside_window(
            lead.created_at, self.zone, self.window
        )
        body = step.after_hours_body if after_hours and step.after_hours_body else step.body
        subject = (
            step.after_hours_subject if after_hours and step.after_hours_subject else step.subject
        )

        def fill(template: str) -> str:
            return render(
                template,
                name=lead.name,
                company=self.company,
                callback_minutes=self.callback_minutes,
                received_when=received_phrase(lead.created_at, at, self.zone),
                call_when=call_phrase(at, self.zone),
            )

        to = lead.phone if step.channel is Channel.SMS else lead.email
        assert to is not None
        self.session.add(
            OutboxMessage(
                lead_id=lead.id,
                step=step.id,
                channel=step.channel.value,
                to_address=to,
                subject=fill(subject) if subject else None,
                body=fill(body),
                sender=f"MOCK-{step.channel.value}",
                idempotency_key=kinds.send_key(lead.id, step.id),
                transactional=step.transactional,
                sent_at=at,
            )
        )
        self.report.outbox += 1
        self.activity(
            lead,
            "followup_sent",
            f"system:MOCK-{step.channel.value}",
            at,
            {
                "step": step.id,
                "channel": step.channel.value,
                "sender": f"MOCK-{step.channel.value}",
                "variant": "after_hours" if after_hours else "standard",
            },
        )


def _created_at(plan: _Plan, now: datetime, zone: ZoneInfo) -> datetime:
    day = now.astimezone(zone).date() - timedelta(days=plan.age_days)
    created = datetime.combine(day, time(plan.hour, plan.minute), tzinfo=zone).astimezone(
        now.tzinfo
    )
    if created > now - timedelta(minutes=10):
        created -= timedelta(days=1)  # "today, later than now" -> yesterday
    return created


def _build_lead(b: _Builder, plan: _Plan, index: int) -> None:
    now, zone = b.now, b.zone
    created = _created_at(plan, now, zone)
    lead = Lead(
        id=uuid.UUID(int=random.Random(f"lead-{index}").getrandbits(128), version=4),
        name=f"{plan.first} {plan.last}",
        email=email_for(plan.first, plan.last, str(index)) if plan.has_email else None,
        phone=f"+1{plan.area}55501{plan.line:02d}" if plan.has_phone else None,
        source=plan.source,
        message=plan.message,
        created_at=created,
        sequence_state=SequenceState.ACTIVE.value,
    )
    b.session.add(lead)
    b.session.flush()
    b.activity(
        lead, "lead_created", plan.source, created, {"source": plan.source, "channel": "demo_seed"}
    )

    # Scoring (mock AI) a moment after capture.
    result = MockScorer().score(lead_context(lead))
    scored_at = created + timedelta(seconds=0.4)
    lead.score, lead.score_summary = result.score, result.summary
    lead.next_step, lead.score_confidence = result.next_step.value, result.confidence
    lead.score_flags, lead.scored_by, lead.score_provider = list(result.flags), "mock", "mock"
    lead.score_latency_ms, lead.scored_at = 3, scored_at
    b.activity(
        lead,
        "ai_scored",
        "ai:mock",
        scored_at,
        {
            "score": result.score,
            "next_step": result.next_step.value,
            "confidence": result.confidence,
            "flags": list(result.flags),
            "scored_by": "mock",
        },
    )
    b.job(lead, kinds.SCORE_LEAD, kinds.score_key(lead.id), {"lead_id": str(lead.id)},
          run_at=created, status=JobStatus.DONE)  # fmt: skip
    review = result.next_step.value == "human_review"

    # Instant acknowledgment: now, or when the sending window opens. Every outcome below
    # comes from the plan (the seed), never from the clock; times that would fall in the
    # future are pulled back to "just now", so the counts are the same at any time of day.
    ack_at = next_send_time(created, zone, b.window) + timedelta(seconds=plan.latency_s)
    stage = "new" if review else plan.stage
    latest = now - timedelta(minutes=1)

    def past(at: datetime, after: datetime) -> datetime:
        return max(after, min(at, latest))

    human_at = past(ack_at + timedelta(minutes=plan.human_after_min), created)
    decided_at = past(human_at + timedelta(hours=plan.decide_after_h), human_at)

    stopped_at: datetime | None = None  # when automation stopped for this lead
    opted_out_at: datetime | None = None
    if plan.opted_out and lead.phone and stage in ("new", "qualified"):
        opted_out_at = past(ack_at + timedelta(minutes=plan.human_after_min / 3), created)
        stopped_at = opted_out_at

    handoff_reason = AI_HUMAN_REVIEW if review else None
    handoff_at = scored_at if review else None
    if handoff_reason is None and plan.replied and lead.phone and opted_out_at is None:
        reply_at = past(ack_at + timedelta(minutes=plan.human_after_min / 2), created)
        handoff_reason, handoff_at = LEAD_REPLIED, reply_at
        b.activity(lead, "inbound_reply", "lead", reply_at, {"channel": "sms", "body": plan.reply})

    # Follow-up steps: sent, cancelled or still queued.
    first_human = human_at if stage in ("contacted", "won", "lost") else None
    for step in b.sequence.steps:
        due = created + timedelta(minutes=step.delay_minutes)
        at = next_send_time(due, zone, b.window) + timedelta(seconds=plan.latency_s)
        if step.transactional:
            at = ack_at
        key = kinds.followup_key(lead.id, step.id)
        payload = {"lead_id": str(lead.id), "step": step.id, "sequence": b.sequence.name}
        has_channel = lead.phone if step.channel is Channel.SMS else lead.email
        blockers = [
            (t, label)
            for t, label in (
                (handoff_at, f"handoff:{handoff_reason}"),
                (stopped_at, "opted_out"),
                (first_human, "contacted_by_human"),
            )
            if t is not None and t <= at
        ]
        if not has_channel:
            b.job(lead, kinds.FOLLOWUP_SEND, key, payload, run_at=due, status=JobStatus.DONE)
        elif blockers:
            b.job(lead, kinds.FOLLOWUP_SEND, key, payload, run_at=due, status=JobStatus.CANCELLED,
                  attempts=0, error=min(blockers, key=lambda x: x[0])[1])  # fmt: skip
        elif at < now:
            b.send(lead, step, at)
            b.job(lead, kinds.FOLLOWUP_SEND, key, payload, run_at=at, status=JobStatus.DONE)
            if lead.first_response_at is None:
                lead.first_response_at = at
        else:  # in the future: the demo worker will send it when it is due
            b.job(lead, kinds.FOLLOWUP_SEND, key, payload, run_at=due, status=JobStatus.QUEUED,
                  attempts=0)  # fmt: skip

    if handoff_reason and handoff_at:
        lead.sequence_state = SequenceState.PAUSED.value
        task_done = stage in ("contacted", "won", "lost")
        task = Task(
            lead_id=lead.id,
            reason=handoff_reason,
            detail=plan.reply if handoff_reason == LEAD_REPLIED else lead.score_summary,
            status=TaskStatus.DONE.value if task_done else TaskStatus.OPEN.value,
            assignee=plan.operator if task_done or plan.human_after_min > 60 else None,
            created_at=handoff_at,
            done_at=human_at if task_done else None,
        )
        b.session.add(task)
        b.session.flush()
        b.activity(
            lead, "handoff", "system", handoff_at, {"reason": handoff_reason, "task_id": task.id}
        )
        b.report.open_tasks += 0 if task_done else 1
    if opted_out_at is not None:
        lead.opted_out_at = opted_out_at
        lead.sequence_state = SequenceState.STOPPED.value
        b.activity(lead, "opted_out", "inbound:sms", opted_out_at, {"channel": "sms"})
        b.activity(lead, "sequence_stopped", "system", opted_out_at, {"reason": "opted_out"})
        b.report.opted_out += 1

    # Stage history, by a person.
    actor = f"human:{plan.operator}"
    path: list[tuple[str, datetime]] = []
    if stage != "new":
        if stage == "qualified" or (plan.path_via_qualified and stage != "contacted"):
            qualified_at = past(ack_at + timedelta(minutes=plan.human_after_min / 2), created)
            path.append(("qualified", min(qualified_at, human_at)))
        if stage in ("contacted", "won") or (stage == "lost" and not plan.path_via_qualified):
            path.append(("contacted", human_at))
        if stage in ("won", "lost"):
            path.append((stage, decided_at))
    prev = "new"
    for to, at in path:
        reason = plan.lost_reason if to == "lost" else None
        b.activity(lead, "stage_changed", actor, at, {"from": prev, "to": to, "reason": reason})
        if to == "contacted":
            b.activity(lead, "human_contact", actor, at, {"channel": "call", "note": None})
            lead.first_response_at = min(lead.first_response_at or at, at)
        prev = to
    lead.stage = stage
    if stage == "lost":
        lead.lost_reason = plan.lost_reason
    if stage in ("contacted", "won", "lost") and lead.sequence_state == SequenceState.ACTIVE.value:
        lead.sequence_state = SequenceState.STOPPED.value

    # CRM sync for each stage it passed through (mock ids).
    for synced_stage, at in [("new", created + timedelta(seconds=2)), *path]:
        b.job(lead, kinds.CRM_SYNC, kinds.crm_key(lead.id, synced_stage),
              {"lead_id": str(lead.id), "stage": synced_stage},
              run_at=min(at, now), status=JobStatus.DONE)  # fmt: skip
        b.activity(lead, "crm_synced", "system:MOCK-hubspot", min(at, now) + timedelta(seconds=1),
                   {"adapter": "MOCK-hubspot", "stage": synced_stage})  # fmt: skip
    lead.crm_contact_id = f"mock-contact-{5000 + index}"
    lead.crm_deal_id = f"mock-deal-{7000 + index}"
    lead.crm_synced_at = min(path[-1][1], now) if path else created + timedelta(seconds=2)

    b.report.stages[stage] = b.report.stages.get(stage, 0) + 1
    b.report.sources[plan.source] = b.report.sources.get(plan.source, 0) + 1


def _dead_letters(b: _Builder, leads: list[Lead]) -> None:
    """Two CRM syncs that failed every attempt during a (simulated) CRM outage."""
    for lead in leads[:2]:
        at = lead.created_at + timedelta(minutes=30)
        b.job(
            lead,
            kinds.CRM_SYNC,
            kinds.crm_key(lead.id, lead.stage) + ":outage",
            {"lead_id": str(lead.id), "stage": lead.stage},
            run_at=at,
            status=JobStatus.DEAD,
            attempts=5,
            error="CRMError: hubspot POST deals -> 503 (simulated outage in the demo data)",
        )
        b.report.dead_jobs += 1


def seed_demo(
    session_factory: sessionmaker[Session],
    *,
    seed: int,
    count: int,
    now: datetime,
    business_timezone: str,
    window: SendWindow,
    company: str,
    callback_minutes: int = 15,
    sequence: Sequence | None = None,
) -> SeedReport:
    """Insert ``count`` fake leads with their history. The database should be empty."""
    rng = random.Random(seed)
    fake = Faker("en_US")
    fake.seed_instance(seed)
    report = SeedReport(leads=count)
    plans = [_plan(rng, fake) for _ in range(count)]
    with session_factory() as session:
        builder = _Builder(
            session=session,
            sequence=sequence or load_sequence(),
            window=window,
            zone=ZoneInfo(business_timezone),
            company=company,
            callback_minutes=callback_minutes,
            now=now,
            report=report,
        )
        for i, plan in enumerate(plans):
            _build_lead(builder, plan, i)
        session.flush()
        oldest = sorted(
            session.query(Lead).filter(Lead.stage == LeadStage.LOST.value),
            key=lambda x: x.created_at,
        )
        _dead_letters(builder, oldest)
        session.commit()
    return report
