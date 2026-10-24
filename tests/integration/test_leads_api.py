from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import create_app
from app.models import Activity, Lead
from tests.conftest import make_settings, valid_lead


def _activities(client: TestClient) -> list[Activity]:
    with client.app.state.session_factory() as s:  # type: ignore[attr-defined]
        return list(s.scalars(select(Activity).order_by(Activity.id)))


def test_create_lead_happy_path(client: TestClient) -> None:
    r = client.post("/api/leads", json=valid_lead())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "Jordan Alvarez"
    assert body["stage"] == "new"
    assert body["source"] == "api"
    assert "phone" not in body
    assert "email" not in body
    assert body["scoring"]["scored_by"] == "mock"
    assert 0 <= body["scoring"]["score"] <= 100

    with client.app.state.session_factory() as s:  # type: ignore[attr-defined]
        lead = s.scalars(select(Lead)).one()
        assert lead.phone == "+12025550123"
        assert lead.score_latency_ms is not None
        assert lead.score_provider == "mock"
    kinds = [a.kind for a in _activities(client)]
    assert kinds == ["lead_created", "ai_scored"]
    assert _activities(client)[0].detail["source"] == "api"


def test_validation_errors_return_422(client: TestClient) -> None:
    for bad in (
        valid_lead(name="🎉🎉 FREE 🎉🎉"),
        valid_lead(phone="123", email=None),
        valid_lead(email="nope"),
        {"name": "Jordan Alvarez"},
        valid_lead(message="m" * 5000),
    ):
        r = client.post("/api/leads", json=bad)
        assert r.status_code == 422, bad
    assert client.post("/api/leads", content=b"not json").status_code == 422


def test_ip_rate_limit_returns_429_with_retry_after() -> None:
    app = create_app(make_settings(rate_limit_capacity=2, rate_limit_refill_per_minute=1))
    with TestClient(app) as c:
        assert c.post("/api/leads", json=valid_lead(phone="202-555-0101")).status_code == 201
        assert c.post("/api/leads", json=valid_lead(phone="202-555-0102")).status_code == 201
        r = c.post("/api/leads", json=valid_lead(phone="202-555-0103"))
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) >= 1


def test_forwarded_for_only_used_when_trusted() -> None:
    app = create_app(
        make_settings(
            rate_limit_capacity=1, rate_limit_refill_per_minute=1, trust_forwarded_for=True
        )
    )
    with TestClient(app) as c:
        h1 = {"X-Forwarded-For": "198.51.100.7, 203.0.113.1"}
        h2 = {"X-Forwarded-For": "198.51.100.7, 203.0.113.2"}
        assert c.post("/api/leads", json=valid_lead(), headers=h1).status_code == 201
        assert (
            c.post("/api/leads", json=valid_lead(phone="202-555-0104"), headers=h2).status_code
            == 201
        )
        assert c.post("/api/leads", json=valid_lead(), headers=h1).status_code == 429


def test_phone_daily_cap(client: TestClient) -> None:
    for _ in range(3):
        assert client.post("/api/leads", json=valid_lead()).status_code == 201
    r = client.post("/api/leads", json=valid_lead(phone="+1 202 555 0123"))
    assert r.status_code == 429
    assert "phone" in r.json()["detail"]
    assert int(r.headers["Retry-After"]) <= 86400
    assert client.post("/api/leads", json=valid_lead(phone="202-555-0188")).status_code == 201
