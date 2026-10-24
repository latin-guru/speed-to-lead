from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app.main import create_app
from tests.conftest import make_settings


def test_healthz_ok(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "checks": {"db": "ok"}}


def test_readyz_ok(client: TestClient) -> None:
    r = client.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["warnings"] == []


def test_readyz_warns_when_webhook_unconfigured() -> None:
    app = create_app(make_settings(meta_app_secret=None, meta_verify_token=None))
    with TestClient(app) as c:
        body = c.get("/readyz").json()
    assert body["warnings"]


def test_health_reports_db_down() -> None:
    app = create_app(make_settings())
    with TestClient(app) as c:
        app.state.engine = create_engine("sqlite:////nonexistent-dir/x.db")
        assert c.get("/healthz").status_code == 503
        assert c.get("/readyz").status_code == 503
