"""Select the scorer from ``AI_PROVIDER``."""

from __future__ import annotations

import httpx

from app.ai.base import AIScorer
from app.ai.providers import ClaudeScorer, GrokScorer
from app.ai.rules import MockScorer
from app.config import Settings


class ProviderConfigError(ValueError):
    pass


def build_scorer(settings: Settings, client: httpx.Client | None = None) -> AIScorer:
    provider = settings.ai_provider
    if provider == "mock":
        return MockScorer()
    http = client or httpx.Client(timeout=settings.ai_timeout_seconds)
    if provider == "claude":
        if settings.anthropic_api_key is None or not settings.anthropic_api_key.get_secret_value():
            raise ProviderConfigError("AI_PROVIDER=claude requires ANTHROPIC_API_KEY")
        return ClaudeScorer(
            client=http,
            api_key=settings.anthropic_api_key.get_secret_value(),
            model=settings.anthropic_model,
            company=settings.company_name,
            timeout=settings.ai_timeout_seconds,
            base_url=settings.anthropic_base_url,
        )
    if provider == "grok":
        if settings.xai_api_key is None or not settings.xai_api_key.get_secret_value():
            raise ProviderConfigError("AI_PROVIDER=grok requires XAI_API_KEY")
        return GrokScorer(
            client=http,
            api_key=settings.xai_api_key.get_secret_value(),
            model=settings.xai_model,
            company=settings.company_name,
            timeout=settings.ai_timeout_seconds,
            base_url=settings.xai_base_url,
        )
    raise ProviderConfigError(f"unknown AI_PROVIDER: {provider!r}")  # pragma: no cover
