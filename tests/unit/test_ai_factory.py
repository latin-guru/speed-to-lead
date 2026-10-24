import pytest
from pydantic import SecretStr

from app.ai.factory import ProviderConfigError, build_scorer
from app.ai.providers import ClaudeScorer, GrokScorer
from app.ai.rules import MockScorer
from tests.conftest import make_settings


def test_default_is_mock() -> None:
    assert isinstance(build_scorer(make_settings()), MockScorer)


def test_claude_selected_with_key() -> None:
    s = make_settings(ai_provider="claude", anthropic_api_key=SecretStr("k-test"))
    assert isinstance(build_scorer(s), ClaudeScorer)


def test_grok_selected_with_key() -> None:
    s = make_settings(ai_provider="grok", xai_api_key=SecretStr("k-test"))
    assert isinstance(build_scorer(s), GrokScorer)


@pytest.mark.parametrize("provider", ["claude", "grok"])
def test_missing_key_fails_fast(provider: str) -> None:
    with pytest.raises(ProviderConfigError):
        build_scorer(make_settings(ai_provider=provider))


def test_unknown_provider_rejected_by_settings() -> None:
    with pytest.raises(ValueError, match="ai_provider"):
        make_settings(ai_provider="other")


def test_prod_requires_secret_key() -> None:
    with pytest.raises(ValueError, match="SECRET_KEY"):
        make_settings(environment="prod", secret_key=None)
