from app.ai.rules import MockScorer, RuleBasedScorer
from app.ai.schemas import NextStep
from tests.unit.ai_helpers import ctx


def test_deterministic() -> None:
    lead = ctx()
    assert RuleBasedScorer().score(lead) == RuleBasedScorer().score(lead)
    assert MockScorer().name == "mock"


def test_urgent_with_phone_calls_now() -> None:
    r = RuleBasedScorer().score(ctx(message="Roof is leaking after the hail storm!"))
    assert r.next_step is NextStep.CALL_NOW
    assert "urgent_need" in r.flags
    assert r.score >= 75


def test_spam_disqualified() -> None:
    r = RuleBasedScorer().score(ctx(message="We sell SEO backlinks for your site"))
    assert r.next_step is NextStep.DISQUALIFY
    assert r.score <= 10


def test_thin_email_only_lead_nurtures() -> None:
    r = RuleBasedScorer().score(ctx(message="hi", has_phone=False, email_domain="gmail.com"))
    assert r.next_step in {NextStep.NURTURE, NextStep.DISQUALIFY}
    assert "thin_message" in r.flags


def test_high_score_without_phone_sends_info() -> None:
    r = RuleBasedScorer().score(
        ctx(
            has_phone=False,
            source="meta_ads",
            message="Want a solar install estimate",
            email_domain="example.com",
        )
    )
    assert r.next_step in {NextStep.SEND_INFO, NextStep.NURTURE}
    assert "custom_email_domain" in r.flags
