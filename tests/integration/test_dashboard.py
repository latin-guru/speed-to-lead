"""Dashboard pages and the metrics JSON endpoints (as an admin unless a test says otherwise)."""

import re
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import Activity, AuditEvent, UserRole
from tests.conftest import ADMIN, drain, seed_lead
from tests.web_helpers import app_of, csrf, login_as, make_client, post, seeded

PHONE_E164 = "+12025550123"
PHONE_PRETTY = "(202) 555-0123"
EMAIL = "jordan.alvarez@example.com"


@pytest.fixture
def web() -> Iterator[TestClient]:
    yield from make_client()


def login(c: TestClient) -> None:
    login_as(c, UserRole.ADMIN)


_seeded = seeded


# --------------------------------------------------------------------------- pages


def test_overview_renders_kpis(web: TestClient) -> None:
    _seeded(web)
    login(web)
    r = web.get("/dashboard")
    assert r.status_code == 200
    for label in (
        "Leads, 7 days",
        "Median first response",
        "Win rate, 30 days",
        "Average lead score",
        "Open handoffs",
        "Dead-letter jobs",
        "Pipeline funnel",
        "Leads by source",
        "Lead score distribution",
        "First-response time by day",
        "Recent activity",
    ):
        assert label in r.text
    assert re.search(r'data-kpi="leads">3<', r.text)
    assert 'integrity="sha384-' in r.text  # vendored Chart.js still pinned with SRI
    assert web.get("/static/vendor/chart.umd.min.js").status_code == 200
    assert "Demo with sample data." in r.text
    assert "May take a moment to wake up." in r.text


def test_lead_list_masks_contacts_and_filters(web: TestClient) -> None:
    _seeded(web)
    login(web)
    r = web.get("/dashboard/leads")
    assert r.status_code == 200
    assert "Jordan Alvarez" in r.text
    for raw in (PHONE_E164, PHONE_PRETTY, EMAIL, "555-0123"):
        assert raw not in r.text
    assert "3 matching" in r.text
    assert "0 matching" in web.get("/dashboard/leads?stage=won").text
    assert "3 matching" in web.get("/dashboard/leads?stage=bogus&source=nope").text  # ignored
    assert "3 matching" in web.get("/dashboard/leads?min_score=0&max_score=abc").text
    assert "0 matching" in web.get("/dashboard/leads?min_score=99").text


def test_lead_list_pages(web: TestClient) -> None:
    app = app_of(web)
    for i in range(27):
        seed_lead(app, phone=f"(303) 555-01{i:02d}", email=f"p{i}@example.com")
    login(web)
    first = web.get("/dashboard/leads")
    assert "Page 1 of 2" in first.text
    assert 'rel="next"' in first.text
    second = web.get("/dashboard/leads?page=2")
    assert 'rel="prev"' in second.text


def test_lead_detail_masked_by_default_and_reveal_is_logged(web: TestClient) -> None:
    lead_id = _seeded(web, 1)[0]
    login(web)
    r = web.get(f"/dashboard/leads/{lead_id}")
    assert r.status_code == 200
    for raw in (PHONE_E164, EMAIL, "555-0123"):
        assert raw not in r.text
    assert "***-***-0123" in r.text
    assert "j***@example.com" in r.text
    for section in ("Lead score", "Timeline", "Outbox", "Tasks", "Change stage", "CRM sync"):
        assert section in r.text
    assert "MOCK" in r.text
    assert "Sent ack_sms" in r.text

    assert (
        web.get(f"/dashboard/leads/{lead_id}?reveal=1").text.count(PHONE_E164) == 0
    )  # GET never reveals
    revealed = post(web, f"/dashboard/leads/{lead_id}/reveal")
    assert revealed.status_code == 200
    assert PHONE_E164 in revealed.text
    assert EMAIL in revealed.text
    with app_of(web).state.session_factory() as s:
        acts = s.scalars(
            select(Activity).where(Activity.lead_id == lead_id, Activity.kind == "contact_revealed")
        ).all()
        events = s.scalars(select(AuditEvent).where(AuditEvent.action == "contact_revealed")).all()
    assert [a.actor for a in acts] == ["human:admin"]
    assert [(e.actor, e.target_id, e.outcome) for e in events] == [
        ("admin@example.com", str(lead_id), "ok")
    ]
    assert web.get(f"/dashboard/leads/{uuid.uuid4()}").status_code == 404
    assert post(web, f"/dashboard/leads/{uuid.uuid4()}/reveal").status_code == 404


def test_stage_form_moves_and_reopens(web: TestClient) -> None:
    lead_id = _seeded(web, 1)[0]
    login(web)
    url = f"/dashboard/leads/{lead_id}/stage"
    assert web.post(url, data={"stage": "qualified"}).status_code == 403  # no CSRF token
    csrf(web)
    r = web.post(url, data={"stage": "lost", "csrf_token": web.cookies.get("stl_csrf")})
    assert r.headers["location"].endswith("?error=reason_required")
    assert "A reason is required" in web.get(r.headers["location"]).text
    r = web.post(
        url,
        data={"stage": "lost", "reason": "went quiet", "csrf_token": web.cookies.get("stl_csrf")},
    )
    assert r.headers["location"].endswith("?moved=lost")
    assert "Moved to lost" in web.get(r.headers["location"]).text
    r = web.post(
        url,
        data={
            "stage": "qualified",
            "reason": "called back",
            "csrf_token": web.cookies.get("stl_csrf"),
        },
    )
    assert r.headers["location"].endswith("?moved=qualified")
    r = web.post(url, data={"stage": "archived", "csrf_token": web.cookies.get("stl_csrf")})
    assert r.headers["location"].endswith("?error=invalid_transition")
    page = web.get(f"/dashboard/leads/{lead_id}").text
    assert "Reopened from lost" in page
    r = web.post(url, data={"stage": "won", "csrf_token": web.cookies.get("stl_csrf")})
    assert "is final" in web.get(f"/dashboard/leads/{lead_id}").text


# --------------------------------------------------------------------------- JSON


def test_metrics_api_needs_auth(web: TestClient) -> None:
    for path in ("summary", "kpis", "funnel", "sources", "scores", "response-times", "activity"):
        assert web.get(f"/api/metrics/{path}").status_code == 401
        assert (
            web.get(f"/api/metrics/{path}", headers={"Authorization": "Bearer nope"}).status_code
            == 401
        )
        assert web.get(f"/api/metrics/{path}", headers=ADMIN).status_code == 200


def test_summary_json_refreshes_with_new_leads(web: TestClient) -> None:
    _seeded(web, 2)
    login(web)
    first = web.get("/api/metrics/summary").json()  # cookie auth, as the page's fetch does
    assert first["kpis"]["leads"] == 2
    assert first["kpis"]["median_first_response_seconds"] is not None
    assert [s["stage"] for s in first["funnel"]["steps"]] == [
        "new",
        "qualified",
        "contacted",
        "won",
    ]
    assert first["sources"] == [{"label": "api", "count": 2}]
    assert len(first["scores"]) == 10
    assert len(first["response_times"]) >= 14
    assert first["activity"][0]["summary"]
    assert not any("555-01" in a["summary"] or "@" in a["summary"] for a in first["activity"])
    seed_lead(app_of(web), phone="(202) 555-0177", email="new@example.com")
    drain(web)
    second = web.get("/api/metrics/summary").json()
    assert second["kpis"]["leads"] == 3
    assert second["generated_at"] >= first["generated_at"]


def test_metrics_endpoints_shapes(web: TestClient) -> None:
    _seeded(web, 2)
    kpis = web.get("/api/metrics/kpis", headers=ADMIN).json()
    assert set(kpis) == {
        "period_days",
        "leads",
        "leads_previous",
        "median_first_response_seconds",
        "win_rate",
        "average_score",
        "open_tasks",
        "dead_jobs",
    }
    assert web.get("/api/metrics/funnel", headers=ADMIN).json()["steps"][0]["reached"] == 2
    assert web.get("/api/metrics/sources?days=1", headers=ADMIN).json()[0]["count"] == 2
    assert sum(b["count"] for b in web.get("/api/metrics/scores", headers=ADMIN).json()) == 2
    assert len(web.get("/api/metrics/response-times?days=3", headers=ADMIN).json()) in (3, 4)
    assert len(web.get("/api/metrics/activity?limit=2", headers=ADMIN).json()) == 2
    assert web.get("/api/metrics/sources?days=0", headers=ADMIN).status_code == 422


def test_metrics_are_documented_in_openapi(web: TestClient) -> None:
    spec = web.get("/openapi.json").json()
    paths = set(spec["paths"])
    for p in ("summary", "kpis", "funnel", "sources", "scores", "response-times", "activity"):
        assert f"/api/metrics/{p}" in paths
    assert "/dashboard" not in paths  # HTML pages stay out of the API docs
    assert "SummaryOut" in spec["components"]["schemas"]


def test_health_alias_matches_healthz(web: TestClient) -> None:
    assert web.get("/health").json() == web.get("/healthz").json()


def test_lead_detail_shows_who_scored_in_plain_words(web: TestClient) -> None:
    """Display only: the stored scored_by values stay mock/fallback/claude."""
    from app.models import Lead

    lead_id = _seeded(web, 1)[0]
    login(web)
    factory = app_of(web).state.session_factory
    for stored, shown in (
        ("mock", "Scored by rules"),
        ("fallback", "Scored by rules (fallback)"),
        ("claude", "Scored by claude"),
    ):
        with factory() as s:
            s.get(Lead, lead_id).scored_by = stored
            s.commit()
        page = web.get(f"/dashboard/leads/{lead_id}").text
        assert shown in page
        with factory() as s:
            assert s.get(Lead, lead_id).scored_by == stored


def test_timeline_and_feed_say_rules_not_mock(web: TestClient) -> None:
    """The scorer is rule-based; screens must not read as "by mock" / "ai:mock"."""
    from app.models import Activity

    lead_id = _seeded(web, 1)[0]
    login(web)
    page = web.get(f"/dashboard/leads/{lead_id}").text
    assert "by rules, suggests" in page
    assert '<span class="who muted">rules</span>' in page
    assert "by mock" not in page
    assert "ai:mock" not in page

    feed = web.get("/api/metrics/activity").json()
    scored = [a for a in feed if a["kind"] == "ai_scored"]
    assert scored, feed
    assert all(a["actor"] == "rules" and " by rules, " in a["summary"] for a in scored)

    with app_of(web).state.session_factory() as s:  # stored values are untouched
        stored = s.scalars(select(Activity).where(Activity.kind == "ai_scored")).all()
        assert stored
        assert all(a.actor == "ai:mock" for a in stored)
        assert all(a.detail["scored_by"] == "mock" for a in stored)
