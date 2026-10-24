"""Hosting the demo publicly: DEMO_ROLES=viewer, unsafe-config refusals, the Render Blueprint."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select

from app.config import DEFAULT_DEMO_PASSWORD, Settings
from app.main import create_app
from app.models import Activity, AuditEvent, Job, Lead, Task, User
from tests.conftest import make_settings
from tests.web_helpers import app_of, login, post

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def viewer_demo(tmp_path: Any) -> Iterator[TestClient]:
    settings = make_settings(
        demo_mode=True,
        demo_roles="viewer",
        demo_leads=30,
        worker_poll_seconds=0.05,
        database_url=f"sqlite:///{tmp_path / 'demo.db'}",
    )
    app = create_app(settings, demo_simulate=False)
    with TestClient(app, base_url="http://localhost", follow_redirects=False) as c:
        yield c


def _snapshot(c: TestClient) -> dict[str, Any]:
    """Everything a person could change. (The in-process worker keeps running seeded jobs,
    so automated rows are left out.)"""
    with app_of(c).state.session_factory() as s:
        return {
            "stages": sorted((str(i), st) for i, st in s.execute(select(Lead.id, Lead.stage))),
            "assignees": sorted(str(a) for a in s.scalars(select(Lead.assignee_id))),
            "closed_tasks": sorted(s.scalars(select(Task.id).where(Task.status != "open"))),
            "dead_jobs": sorted(s.scalars(select(Job.id).where(Job.status == "dead"))),
            "users": sorted(s.execute(select(User.email, User.role, User.active)).all()),
            "human_activities": s.scalar(
                select(func.count(Activity.id)).where(Activity.actor.like("human:%"))
            ),
        }


def test_viewer_only_demo_offers_only_the_viewer_login(viewer_demo: TestClient) -> None:
    page = viewer_demo.get("/login").text
    assert "viewer@example.com" in page
    assert "admin@example.com" not in page
    assert "rep@example.com" not in page
    assert "two-factor on" not in page  # the admin note only shows when admin is offered
    assert "read-only account" in page
    with app_of(viewer_demo).state.session_factory() as s:
        assert list(s.scalars(select(User.email))) == ["viewer@example.com"]
    for email in ("admin@example.com", "rep@example.com"):
        r = login(viewer_demo, email, DEFAULT_DEMO_PASSWORD)
        assert r.status_code == 401, email
    assert login(viewer_demo, "viewer@example.com", DEFAULT_DEMO_PASSWORD).status_code == 303


def test_viewer_cannot_change_anything(viewer_demo: TestClient) -> None:
    c = viewer_demo
    assert login(c, "viewer@example.com", DEFAULT_DEMO_PASSWORD).status_code == 303
    with app_of(c).state.session_factory() as s:
        lead_id = s.scalars(select(Lead.id).where(Lead.stage == "new")).first()
        task_id = s.scalars(select(Task.id).where(Task.status == "open")).first()
        job_id = s.scalars(select(Job.id).where(Job.status == "dead")).first()
        viewer_id = s.scalar(select(User.id))
    assert lead_id is not None
    assert task_id is not None
    assert job_id is not None
    before = _snapshot(c)
    attempts: list[tuple[str, dict[str, str]]] = [
        (f"/dashboard/leads/{lead_id}/reveal", {}),
        (f"/dashboard/leads/{lead_id}/stage", {"stage": "qualified"}),
        (f"/dashboard/leads/{lead_id}/assign", {"assignee": "me"}),
        (f"/dashboard/tasks/{task_id}/close", {}),
        (f"/dashboard/admin/jobs/{job_id}/retry", {}),
        ("/dashboard/admin/kill-switch", {"paused": "1"}),
        (
            "/dashboard/admin/users",
            {"email": "x@example.com", "role": "admin", "password": "p" * 9},
        ),
        (f"/dashboard/admin/users/{viewer_id}", {"role": "admin", "active": "1"}),
        (f"/dashboard/admin/users/{viewer_id}/password", {"password": "q" * 16}),
        (f"/dashboard/admin/users/{viewer_id}/2fa-reset", {}),
        ("/dashboard/account/2fa/start", {}),
    ]
    for url, data in attempts:
        assert post(c, url, data).status_code == 403, url
    # The lead page never offers the write controls to a viewer.
    detail = c.get(f"/dashboard/leads/{lead_id}").text
    assert "Show contact details" not in detail
    assert _snapshot(c) == before
    with app_of(c).state.session_factory() as s:
        actions = set(s.scalars(select(AuditEvent.action)))
    assert not actions & {"contact_revealed", "stage_changed", "kill_switch", "user_updated"}


def test_demo_mode_refuses_unsafe_config() -> None:
    with pytest.raises(ValidationError, match="only runs on SQLite"):
        make_settings(demo_mode=True, database_url="postgresql+psycopg://u@db/x")
    with pytest.raises(ValidationError, match="DEMO_ALLOW_REAL_AI"):
        make_settings(
            demo_mode=True, ai_provider="claude", anthropic_api_key=SecretStr("not-a-real-key")
        )
    allowed = make_settings(
        demo_mode=True,
        ai_provider="claude",
        anthropic_api_key=SecretStr("not-a-real-key"),
        demo_allow_real_ai=True,
    )
    assert allowed.ai_provider == "claude"  # an explicit opt-in only
    for provider in ("hubspot", "gohighlevel", "salesforce"):
        with pytest.raises(ValidationError, match="MOCK CRMs"):
            make_settings(demo_mode=True, crm_provider=provider, crm_mock=False)
    assert make_settings(demo_mode=True, crm_provider="none", crm_mock=False).demo_mode
    # Outside demo mode the same settings are fine.
    assert make_settings(crm_provider="salesforce", crm_mock=False).crm_mock is False


def _blueprint() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((ROOT / "render.yaml").read_text())
    return data


def test_render_blueprint_is_a_safe_free_demo(monkeypatch: pytest.MonkeyPatch) -> None:
    services = _blueprint()["services"]
    assert len(services) == 1
    web = services[0]
    assert web["type"] == "web"
    assert web["plan"] == "free"
    assert web["runtime"] == "python"
    assert web["healthCheckPath"] == "/healthz"
    assert web["buildCommand"] == "pip install -r requirements.txt"
    assert "app.main:create_app --factory" in web["startCommand"]
    assert "--port $PORT" in web["startCommand"]
    env = {e["key"]: e for e in web["envVars"]}
    assert env["DEMO_MODE"]["value"] == "1"
    assert env["DEMO_ROLES"]["value"] == "viewer"
    assert env["AI_PROVIDER"]["value"] == "mock"
    assert env["CRM_MOCK"]["value"] == "true"
    assert env["DATABASE_URL"]["value"].startswith("sqlite:///")
    for key, entry in env.items():
        if any(word in key for word in ("SECRET", "TOKEN", "KEY", "PASSWORD")):
            assert entry.get("generateValue") is True, key  # never a literal secret
    # The Blueprint's env builds valid demo settings (generated values stand in for Render's).
    for key, entry in env.items():
        value = "generated-" + "x" * 40 if entry.get("generateValue") else str(entry["value"])
        monkeypatch.setenv(key, value)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.demo_mode
    assert settings.demo_role_list == ["viewer"]
    assert settings.ai_provider == "mock"
