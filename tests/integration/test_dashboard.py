"""Dashboard pages, login/session cookie, metrics JSON endpoints."""

import re
import uuid
from collections.abc import Iterator
from http.cookies import SimpleCookie
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import create_app
from app.models import Activity
from app.security.session import SESSION_COOKIE
from tests.conftest import ADMIN, ADMIN_TOKEN, drain, make_settings, seed_lead

PHONE_E164 = "+12025550123"
PHONE_PRETTY = "(202) 555-0123"
EMAIL = "jordan.alvarez@example.com"


def _client(base_url: str = "http://localhost", **overrides: Any) -> Iterator[TestClient]:
    app = create_app(make_settings(**overrides))
    with TestClient(app, base_url=base_url, follow_redirects=False) as c:
        yield c


@pytest.fixture
def web() -> Iterator[TestClient]:
    yield from _client()


def _csrf(c: TestClient) -> str:
    c.get("/login")
    token = c.cookies.get("stl_csrf")
    assert token
    return token


def login(c: TestClient, token: str = ADMIN_TOKEN, next_path: str = "/dashboard") -> Any:
    return c.post("/login", data={"token": token, "csrf_token": _csrf(c), "next": next_path})


def _seeded(c: TestClient, n: int = 3) -> list[uuid.UUID]:
    app: FastAPI = c.app  # type: ignore[assignment]
    ids = [seed_lead(app)]
    ids += [
        seed_lead(app, phone=f"(202) 555-01{40 + i}", email=f"lead{i}@example.com")
        for i in range(n - 1)
    ]
    drain(c)
    return ids


def _set_cookie(response: Any, name: str) -> SimpleCookie:
    for header in response.headers.get_list("set-cookie"):
        if header.startswith(name + "="):
            jar: SimpleCookie = SimpleCookie()
            jar.load(header)
            return jar
    raise AssertionError(f"no {name} cookie set")


# --------------------------------------------------------------------------- login


def test_pages_redirect_to_login(web: TestClient) -> None:
    r = web.get("/dashboard/leads?stage=new")
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=%2Fdashboard%2Fleads%3Fstage%3Dnew"
    page = web.get("/login")
    assert page.status_code == 200
    assert "Access token" in page.text
    assert "Demo credentials" not in page.text  # not demo mode
    assert "DEMO" in page.text  # the banner is always there
    assert "Content-Security-Policy" in page.headers
    assert "script-src 'self'" in page.headers["content-security-policy"]


def test_login_sets_a_hardened_session_cookie(web: TestClient) -> None:
    r = login(web, next_path="/dashboard/leads")
    assert r.status_code == 303
    assert r.headers["location"] == "/dashboard/leads"
    morsel = _set_cookie(r, SESSION_COOKIE)[SESSION_COOKIE]
    assert morsel["httponly"]
    assert morsel["samesite"] == "strict"
    assert not morsel["secure"]  # plain-HTTP localhost development
    assert morsel["path"] == "/"
    assert ADMIN_TOKEN not in morsel.value  # the token itself is never in the cookie
    assert web.get("/dashboard").status_code == 200
    assert web.get("/login").status_code == 303  # already logged in


def test_cookie_is_secure_off_localhost() -> None:
    for c in _client(base_url="https://dash.example.com"):
        assert _set_cookie(c.get("/login"), "stl_csrf")["stl_csrf"]["secure"]
        r = login(c)
        assert r.status_code == 303
        assert _set_cookie(r, SESSION_COOKIE)[SESSION_COOKIE]["secure"]


def test_login_failures(web: TestClient) -> None:
    r = web.post("/login", data={"token": ADMIN_TOKEN, "csrf_token": "forged"})
    assert r.status_code == 403
    r = login(web, token="wrong-token")
    assert r.status_code == 401
    assert "not valid" in r.text
    assert SESSION_COOKIE not in r.headers.get("set-cookie", "")
    assert web.get("/dashboard").status_code == 303


def test_login_without_any_token_configured() -> None:
    for c in _client(admin_token=None):
        assert "No login is configured" in c.get("/login").text
        assert login(c, token="").status_code == 401


@pytest.mark.parametrize(
    "target", ["//evil.example.com", "https://evil.example.com", "/docs", "/dashboard\\x"]
)
def test_login_never_redirects_off_the_dashboard(web: TestClient, target: str) -> None:
    assert login(web, next_path=target).headers["location"] == "/dashboard"


def test_tampered_or_rotated_session_is_rejected(web: TestClient) -> None:
    login(web)
    cookie = web.cookies.get(SESSION_COOKIE)
    assert cookie
    _role, *rest = cookie.split(".")
    web.cookies.set(SESSION_COOKIE, ".".join(["admin", str(int(rest[0]) + 999), *rest[1:]]))
    assert web.get("/dashboard").status_code == 303
    web.cookies.set(SESSION_COOKIE, cookie)
    assert web.get("/dashboard").status_code == 200
    web.app.state.settings.admin_token = None  # type: ignore[attr-defined]  # token rotated away
    assert web.get("/dashboard").status_code == 303


def test_logout(web: TestClient) -> None:
    login(web)
    assert web.post("/logout", data={"csrf_token": "x"}).status_code == 403
    r = web.post("/logout", data={"csrf_token": web.cookies.get("stl_csrf")})
    assert r.status_code == 303
    assert 'stl_session=""' in r.headers["set-cookie"] or "Max-Age=0" in r.headers["set-cookie"]


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
        "Average AI score",
        "Open handoffs",
        "Dead-letter jobs",
        "Pipeline funnel",
        "Leads by source",
        "AI score distribution",
        "First-response time by day",
        "Recent activity",
    ):
        assert label in r.text
    assert re.search(r'data-kpi="leads">3<', r.text)
    assert 'integrity="sha384-' in r.text  # vendored Chart.js still pinned with SRI
    assert web.get("/static/vendor/chart.umd.min.js").status_code == 200
    assert "fictional company, fake data, no real messages are sent" in r.text


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
    app: FastAPI = web.app  # type: ignore[assignment]
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
    for section in ("AI assessment", "Timeline", "Outbox", "Tasks", "Change stage", "CRM sync"):
        assert section in r.text
    assert "MOCK" in r.text
    assert "Sent ack_sms" in r.text

    revealed = web.get(f"/dashboard/leads/{lead_id}?reveal=1")
    assert PHONE_E164 in revealed.text
    assert EMAIL in revealed.text
    with web.app.state.session_factory() as s:  # type: ignore[attr-defined]
        acts = s.scalars(
            select(Activity).where(Activity.lead_id == lead_id, Activity.kind == "contact_revealed")
        ).all()
    assert [a.actor for a in acts] == ["human:dashboard-admin"]
    assert web.get(f"/dashboard/leads/{uuid.uuid4()}").status_code == 404


def test_stage_form_moves_and_reopens(web: TestClient) -> None:
    lead_id = _seeded(web, 1)[0]
    login(web)
    url = f"/dashboard/leads/{lead_id}/stage"
    assert web.post(url, data={"stage": "qualified"}).status_code == 403  # no CSRF token
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
    seed_lead(web.app, phone="(202) 555-0177", email="new@example.com")  # type: ignore[arg-type]
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
