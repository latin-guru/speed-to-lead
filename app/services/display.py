"""Plain-word labels for screens. Display only: stored values never change.

The built-in scorer is rule-based but is stored as ``mock`` (``MockScorer``), and its
activity actor as ``ai:mock``. People should read "rules" instead.
"""

from __future__ import annotations

_SCORER_LABELS = {"mock": "rules", "rules": "rules", "fallback": "rules (fallback)"}


def display_scorer(name: object) -> str:
    """``mock``/``rules`` -> "rules", ``fallback`` -> "rules (fallback)", anything else as is."""
    text = "" if name is None else str(name)
    return _SCORER_LABELS.get(text, text)


def display_actor(actor: object) -> str:
    """``ai:mock`` -> "rules", ``ai:fallback`` -> "rules (fallback)"; other actors unchanged."""
    text = "" if actor is None else str(actor)
    if text.startswith("ai:"):
        name = text[3:]
        if name in _SCORER_LABELS:
            return _SCORER_LABELS[name]
    return text
