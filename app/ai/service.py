"""Scoring orchestration: pre-check -> provider (with retries/breaker) -> fallback -> policy."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass

from app.ai.base import AIScorer, InvalidOutputError, ProviderError
from app.ai.guard import apply_policy, precheck
from app.ai.resilience import CircuitBreaker, call_with_retries
from app.ai.rules import RuleBasedScorer
from app.ai.schemas import LeadContext, ScoreResult

log = logging.getLogger(__name__)

LOCAL_SCORERS = {"mock", "rules"}


@dataclass(frozen=True)
class ScoreOutcome:
    result: ScoreResult
    scored_by: str  # mock | claude | grok | fallback
    provider: str  # the provider that was configured / attempted
    latency_ms: int
    attempts: int
    error: str | None = None


class ScoringService:
    def __init__(
        self,
        scorer: AIScorer,
        *,
        min_confidence: float = 0.5,
        max_attempts: int = 3,
        budget_seconds: float = 8.0,
        breaker: CircuitBreaker | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[], float] | None = None,
    ) -> None:
        self.scorer = scorer
        self.fallback = RuleBasedScorer()
        self.min_confidence = min_confidence
        self.max_attempts = max_attempts
        self.budget_seconds = budget_seconds
        self.breaker = breaker or CircuitBreaker(threshold=5, cooldown=60.0, clock=clock)
        self._sleep = sleep
        self._clock = clock
        self._rng = rng or random.SystemRandom().random

    def score(self, lead: LeadContext) -> ScoreOutcome:
        started = self._clock()
        pre_flags = precheck(lead)
        if pre_flags:
            log.warning(
                "lead_input_suspicious",
                extra={"lead_id": lead.lead_id, "flags": pre_flags, "sample": lead.message[:80]},
            )

        provider = self.scorer.name
        attempts = 1
        error: str | None = None
        extra_flags: list[str] = []

        if provider in LOCAL_SCORERS:
            raw = self.scorer.score(lead)
            scored_by = provider
        elif not self.breaker.allow():
            raw, scored_by, error = self.fallback.score(lead), "fallback", "circuit_open"
            extra_flags.append("provider_circuit_open")
        else:
            try:
                raw, attempts = call_with_retries(
                    lambda: self.scorer.score(lead),
                    max_attempts=self.max_attempts,
                    budget_seconds=self.budget_seconds,
                    sleep=self._sleep,
                    clock=self._clock,
                    rng=self._rng,
                )
                self.breaker.record_success()
                scored_by = provider
            except InvalidOutputError as exc:
                self.breaker.record_failure()
                raw, scored_by, error = self.fallback.score(lead), "fallback", str(exc)
                extra_flags.append("provider_invalid_output")
            except ProviderError as exc:
                self.breaker.record_failure()
                raw, scored_by, error = self.fallback.score(lead), "fallback", str(exc)
                extra_flags.append("provider_unavailable")
            except Exception as exc:  # defensive: a provider bug must never block intake
                self.breaker.record_failure()
                log.exception("scorer_crashed", extra={"provider": provider})
                raw, scored_by, error = self.fallback.score(lead), "fallback", type(exc).__name__
                extra_flags.append("provider_error")
            if error:
                log.warning(
                    "ai_provider_fallback",
                    extra={"lead_id": lead.lead_id, "provider": provider, "error": error},
                )

        result = apply_policy(
            raw, pre_flags, min_confidence=self.min_confidence, extra_flags=extra_flags
        )
        latency_ms = int((self._clock() - started) * 1000)
        return ScoreOutcome(result, scored_by, provider, latency_ms, attempts, error)
