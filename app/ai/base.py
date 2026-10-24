"""Scorer protocol and provider error types."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.ai.schemas import LeadContext, ScoreResult


@runtime_checkable
class AIScorer(Protocol):
    name: str

    def score(self, lead: LeadContext) -> ScoreResult: ...


class ProviderError(Exception):
    """Provider call failed and should not be retried (bad request, auth, ...)."""


class RetryableProviderError(ProviderError):
    """Timeout, connection error, 5xx or 429: safe to retry with backoff."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class InvalidOutputError(ProviderError):
    """Provider answered, but not with a valid ``ScoreResult`` (even after one repair)."""
