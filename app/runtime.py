"""Build the pieces the API and the worker share, from settings."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import timedelta

import httpx

from app.ai.base import AIScorer
from app.ai.factory import build_scorer
from app.ai.resilience import CircuitBreaker
from app.ai.service import ScoringService
from app.config import Settings
from app.crm.base import CRMAdapter
from app.crm.sync import build_crm_adapter
from app.followups.engine import SendPolicy
from app.followups.guards import SendWindow
from app.followups.senders import mock_senders
from app.followups.sequence import Sequence, load_sequence
from app.interview.mailer import EmailBackend, build_real_sender
from app.interview.routing import EmailCaps, InterviewEmailRouter
from app.jobs.handlers import JobContext


def build_policy(settings: Settings) -> SendPolicy:
    return SendPolicy(
        company=settings.company_name,
        callback_minutes=settings.callback_minutes,
        business_timezone=settings.business_timezone,
        quiet_hours=settings.quiet_hours_enabled,
        window=SendWindow.parse(settings.send_windows),
        max_per_week=settings.max_touches_per_week,
        min_gap=timedelta(hours=settings.min_touch_gap_hours),
        env_paused=settings.sends_paused,
        kill_switch_recheck=timedelta(seconds=settings.kill_switch_recheck_seconds),
    )


def build_scoring(
    settings: Settings,
    *,
    scorer: AIScorer | None = None,
    http_client: httpx.Client | None = None,
    sleep: Callable[[float], None] | None = None,
) -> ScoringService:
    scorer = scorer or build_scorer(settings, http_client)
    breaker = CircuitBreaker(
        threshold=settings.ai_breaker_threshold, cooldown=settings.ai_breaker_cooldown_seconds
    )
    return ScoringService(
        scorer,
        min_confidence=settings.ai_min_confidence,
        max_attempts=settings.ai_max_attempts,
        budget_seconds=settings.ai_total_budget_seconds,
        breaker=breaker,
        sleep=sleep or time.sleep,
    )


def build_job_context(
    settings: Settings,
    *,
    scoring: ScoringService,
    sequence: Sequence | None = None,
    crm: CRMAdapter | None = None,
    email_backend: EmailBackend | None = None,
) -> JobContext:
    return JobContext(
        scoring=scoring,
        sequence=sequence or load_sequence(settings.followup_sequence_path),
        # MOCK senders for everything; the router below may swap in the real email sender
        # for an interview invite's one acknowledgment (never SMS, never anything else).
        senders=mock_senders(),
        policy=build_policy(settings),
        crm=crm if crm is not None else build_crm_adapter(settings),
        pick_sender=build_router(settings, email_backend),
    )


def build_router(settings: Settings, backend: EmailBackend | None = None) -> InterviewEmailRouter:
    return InterviewEmailRouter(
        build_real_sender(settings, backend) if settings.real_email_configured else None,
        EmailCaps(
            per_hour=settings.interview_email_max_per_hour,
            per_day=settings.interview_email_max_per_day,
        ),
    )
