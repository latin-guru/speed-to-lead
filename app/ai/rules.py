"""Deterministic rule-based scorer: the default provider and the fallback for real ones."""

from __future__ import annotations

import re

from app.ai.schemas import LeadContext, NextStep, ScoreResult

_URGENT = re.compile(
    r"\b(leak|leaking|storm|hail|emergency|urgent|asap|damage|damaged|tarp)\b", re.I
)
_INTENT = re.compile(
    r"\b(quote|estimate|replace|replacement|install|installation|solar|panels?|battery|inspection)\b",
    re.I,
)
_NOT_A_LEAD = re.compile(r"\b(seo|backlinks?|crypto|loan offer|guest post|unsubscribe)\b", re.I)
_FREE_MAIL = {"gmail.com", "yahoo.com", "outlook.com", "icloud.com", "aol.com", "proton.me"}


class RuleBasedScorer:
    """Scores from contact completeness, intent keywords and source. No network, no randomness."""

    name = "rules"

    def score(self, lead: LeadContext) -> ScoreResult:
        score = 30
        flags: list[str] = []
        message = lead.message or ""

        if lead.has_phone:
            score += 20
        if lead.has_email:
            score += 10
        if lead.source == "meta":
            score += 5
        urgent = bool(_URGENT.search(message))
        if urgent:
            score += 25
            flags.append("urgent_need")
        if _INTENT.search(message):
            score += 15
        if len(message.strip()) < 10:
            score -= 5
            flags.append("thin_message")
        if lead.email_domain and lead.email_domain not in _FREE_MAIL:
            flags.append("custom_email_domain")
        spam = bool(_NOT_A_LEAD.search(message))
        if spam:
            score = min(score, 10)
            flags.append("likely_spam")

        score = max(0, min(100, score))
        if spam:
            step = NextStep.DISQUALIFY
        elif urgent and lead.has_phone:
            step = NextStep.CALL_NOW
        elif score >= 70:
            step = NextStep.CALL_NOW if lead.has_phone else NextStep.SEND_INFO
        elif score >= 50:
            step = NextStep.SEND_INFO
        elif score >= 25:
            step = NextStep.NURTURE
        else:
            step = NextStep.DISQUALIFY

        summary = "Rule-based score from contact completeness, intent keywords and source."
        if urgent:
            summary = "Mentions an urgent problem. Rule-based score favors a fast call."
        return ScoreResult(
            score=score, summary=summary, next_step=step, confidence=0.6, flags=flags
        )


class MockScorer(RuleBasedScorer):
    """Default ``AI_PROVIDER=mock``: identical rules, reported as ``mock``."""

    name = "mock"
