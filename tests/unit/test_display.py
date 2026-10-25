"""Screen labels for the rule-based scorer. Display only: stored values never change."""

import pytest

from app.services.display import display_actor, display_scorer


@pytest.mark.parametrize(
    ("stored", "shown"),
    [
        ("mock", "rules"),
        ("rules", "rules"),
        ("fallback", "rules (fallback)"),
        ("claude", "claude"),
        ("grok", "grok"),
        ("", ""),
        (None, ""),
    ],
)
def test_display_scorer(stored: object, shown: str) -> None:
    assert display_scorer(stored) == shown


@pytest.mark.parametrize(
    ("stored", "shown"),
    [
        ("ai:mock", "rules"),
        ("ai:fallback", "rules (fallback)"),
        ("ai:claude", "ai:claude"),
        ("human:lee", "human:lee"),
        ("system:MOCK-hubspot", "system:MOCK-hubspot"),
        ("mock", "mock"),  # only the ai: actor form is relabeled
        ("web_form", "web_form"),
        (None, ""),
    ],
)
def test_display_actor(stored: object, shown: str) -> None:
    assert display_actor(stored) == shown
