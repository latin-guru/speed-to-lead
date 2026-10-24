"""Broken access control: every role against every sensitive action, enforced on the server.

Each test logs in as one role and calls the route directly (no UI), including on leads and
tasks that belong to someone else (IDOR: only the id in the URL changes).
"""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models import AuditEvent, Job, JobStatus, Lead, Task, User, UserRole
from tests.conftest import PASSWORD, ROLE_EMAILS, add_user, drain, seed_lead
from tests.web_helpers import app_of, assign, csrf, login, make_client, post

OTHER_REP = "rep2@example.com"


@dataclass
class World:
    c: TestClient
    users: dict[str, int]
    leads: dict[str, uuid.UUID]  # unassigned | mine (the rep's) | other (another rep's)
    tasks: dict[str, int]
    dead_job: int


def _world(c: TestClient) -> World:
    app = app_of(c)
    users = {role.value: add_user(app, role) for role in UserRole}
    users["rep2"] = add_user(app, UserRole.REP, email=OTHER_REP)
    leads = {
        "unassigned": seed_lead(app),
        "mine": seed_lead(app, phone="(202) 555-0150", email="mine@example.com"),
        "other": seed_lead(app, phone="(202) 555-0151", email="other@example.com"),
    }
    drain(c)
    assign(c, leads["mine"], users["rep"])
    assign(c, leads["other"], users["rep2"])
    tasks = {}
    with app.state.session_factory() as s:
        for key, lead_id in leads.items():
            task = Task(lead_id=lead_id, reason="lead_replied")
            s.add(task)
            s.flush()
            tasks[key] = task.id
        job = s.scalars(select(Job)).first()
        assert job is not None
        job.status = JobStatus.DEAD.value
        s.commit()
        dead_job = job.id
    return World(c, users, leads, tasks, dead_job)


@pytest.fixture
def world() -> Iterator[World]:
    for c in make_client():
        yield _world(c)


def _as(w: World, role: str) -> None:
    w.c.cookies.clear()
    email = OTHER_REP if role == "rep2" else ROLE_EMAILS[UserRole(role)]
    assert login(w.c, email=email).status_code == 303


ROLES = ["admin", "rep", "viewer"]


# --------------------------------------------------------------------------- reading


@pytest.mark.parametrize("role", ROLES)
def test_every_role_can_read_the_dashboard_masked(world: World, role: str) -> None:
    _as(world, role)
    assert world.c.get("/dashboard").status_code == 200
    assert world.c.get("/dashboard/leads").status_code == 200
    page = world.c.get(f"/dashboard/leads/{world.leads['mine']}")
    assert page.status_code == 200
    assert "mine@example.com" not in page.text
    assert "5550150" not in page.text
    assert world.c.get("/api/metrics/summary").status_code == 200
    assert ("Show contact details" in page.text) == (role in ("admin", "rep"))


@pytest.mark.parametrize("role", ROLES)
def test_a_dashboard_session_never_opens_the_machine_api(world: World, role: str) -> None:
    _as(world, role)
    assert world.c.get("/admin/jobs").status_code == 401
    assert world.c.get(f"/api/leads/{world.leads['mine']}").status_code == 401
    assert world.c.get("/api/tasks").status_code == 401


# --------------------------------------------------------------------------- reveal (IDOR)

REVEAL = {
    ("admin", "unassigned"): 200,
    ("admin", "mine"): 200,
    ("admin", "other"): 200,
    ("rep", "unassigned"): 403,  # assign it to yourself first
    ("rep", "mine"): 200,
    ("rep", "other"): 403,
    ("viewer", "unassigned"): 403,
    ("viewer", "mine"): 403,
    ("viewer", "other"): 403,
}


@pytest.mark.parametrize(("role", "lead"), sorted(REVEAL))
def test_reveal_contact_details(world: World, role: str, lead: str) -> None:
    _as(world, role)
    lead_id = world.leads[lead]
    r = post(world.c, f"/dashboard/leads/{lead_id}/reveal")
    assert r.status_code == REVEAL[(role, lead)]
    email = {"unassigned": "jordan.alvarez@example.com", "mine": "mine@example.com"}.get(
        lead, "other@example.com"
    )
    assert (email in r.text) == (r.status_code == 200)
    with app_of(world.c).state.session_factory() as s:
        outcomes = list(
            s.scalars(
                select(AuditEvent.outcome).where(
                    AuditEvent.action == "contact_revealed", AuditEvent.target_id == str(lead_id)
                )
            )
        )
    if r.status_code == 200:
        assert outcomes == ["ok"]
    elif role == "viewer":
        assert outcomes == []  # refused by role before the lead is looked at
    else:
        assert outcomes == ["denied"]  # a rep probing another lead is recorded


def test_rep_reveals_after_assigning_to_self(world: World) -> None:
    _as(world, "rep")
    lead_id = world.leads["unassigned"]
    page = world.c.get(f"/dashboard/leads/{lead_id}").text
    assert "Assign this lead to yourself" in page
    r = post(world.c, f"/dashboard/leads/{lead_id}/assign", {"assignee": "me"})
    assert r.status_code == 303
    assert "Assignment saved" in world.c.get(r.headers["location"]).text
    assert post(world.c, f"/dashboard/leads/{lead_id}/reveal").status_code == 200


# --------------------------------------------------------------------------- stage, tasks

WRITE = {
    ("admin", "unassigned"): 303,
    ("admin", "mine"): 303,
    ("admin", "other"): 303,
    ("rep", "unassigned"): 303,
    ("rep", "mine"): 303,
    ("rep", "other"): 403,
    ("viewer", "unassigned"): 403,
    ("viewer", "mine"): 403,
    ("viewer", "other"): 403,
}


@pytest.mark.parametrize(("role", "lead"), sorted(WRITE))
def test_change_stage(world: World, role: str, lead: str) -> None:
    _as(world, role)
    lead_id = world.leads[lead]
    r = post(world.c, f"/dashboard/leads/{lead_id}/stage", {"stage": "qualified"})
    assert r.status_code == WRITE[(role, lead)]
    with app_of(world.c).state.session_factory() as s:
        stage = s.scalar(select(Lead.stage).where(Lead.id == lead_id))
    assert stage == ("qualified" if r.status_code == 303 else "new")


@pytest.mark.parametrize(("role", "lead"), sorted(WRITE))
def test_close_task(world: World, role: str, lead: str) -> None:
    _as(world, role)
    task_id = world.tasks[lead]
    r = post(world.c, f"/dashboard/tasks/{task_id}/close")
    assert r.status_code == WRITE[(role, lead)]
    with app_of(world.c).state.session_factory() as s:
        status = s.scalar(select(Task.status).where(Task.id == task_id))
    assert status == ("done" if r.status_code == 303 else "open")


# --------------------------------------------------------------------------- assignment

ASSIGN_ME = {
    ("admin", "unassigned"): 303,
    ("admin", "other"): 303,
    ("rep", "unassigned"): 303,
    ("rep", "other"): 403,  # no stealing another rep's lead
    ("viewer", "unassigned"): 403,
}


@pytest.mark.parametrize(("role", "lead"), sorted(ASSIGN_ME))
def test_assign_to_me(world: World, role: str, lead: str) -> None:
    _as(world, role)
    lead_id = world.leads[lead]
    r = post(world.c, f"/dashboard/leads/{lead_id}/assign", {"assignee": "me"})
    assert r.status_code == ASSIGN_ME[(role, lead)]
    with app_of(world.c).state.session_factory() as s:
        assignee = s.scalar(select(Lead.assignee_id).where(Lead.id == lead_id))
    if r.status_code == 303:
        assert assignee == world.users[role]


@pytest.mark.parametrize("role", ["rep", "viewer"])
def test_only_admins_assign_others(world: World, role: str) -> None:
    _as(world, role)
    lead_id = world.leads["unassigned"]
    for choice in (str(world.users["rep2"]), ""):
        r = post(world.c, f"/dashboard/leads/{lead_id}/assign", {"assignee": choice})
        assert r.status_code == 403


def test_admin_assigns_and_unassigns(world: World) -> None:
    _as(world, "admin")
    lead_id = world.leads["mine"]
    url = f"/dashboard/leads/{lead_id}/assign"
    page = world.c.get(f"/dashboard/leads/{lead_id}").text
    assert 'name="assignee"' in page
    assert post(world.c, url, {"assignee": str(world.users["rep2"])}).status_code == 303
    assert post(world.c, url, {"assignee": ""}).status_code == 303
    for bad in (str(world.users["viewer"]), "999999", "abc"):
        assert post(world.c, url, {"assignee": bad}).status_code == 422
    with app_of(world.c).state.session_factory() as s:
        assert s.scalar(select(Lead.assignee_id).where(Lead.id == lead_id)) is None


# --------------------------------------------------------------------------- admin only

ADMIN_GETS = [
    "/dashboard/admin/jobs",
    "/dashboard/admin/system",
    "/dashboard/admin/users",
    "/dashboard/admin/audit",
]


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("path", ADMIN_GETS)
def test_admin_pages(world: World, role: str, path: str) -> None:
    _as(world, role)
    r = world.c.get(path)
    assert r.status_code == (200 if role == "admin" else 403)
    if role != "admin":
        assert "Not allowed" in r.text


def _admin_posts(w: World) -> list[tuple[str, dict[str, str]]]:
    rep = w.users["rep2"]
    return [
        (f"/dashboard/admin/jobs/{w.dead_job}/retry", {}),
        ("/dashboard/admin/kill-switch", {"paused": "1"}),
        (
            "/dashboard/admin/users",
            {"email": "new@example.com", "role": "admin", "password": PASSWORD},
        ),
        (f"/dashboard/admin/users/{rep}", {"role": "admin", "active": "1"}),
        (f"/dashboard/admin/users/{rep}/unlock", {}),
        (
            f"/dashboard/admin/users/{rep}/password",
            {"password": "a brand new password"},  # pragma: allowlist secret
        ),
    ]


@pytest.mark.parametrize("role", ["rep", "viewer"])
def test_admin_actions_are_refused_for_other_roles(world: World, role: str) -> None:
    _as(world, role)
    for url, data in _admin_posts(world):
        assert post(world.c, url, data).status_code == 403, url
    with app_of(world.c).state.session_factory() as s:
        assert s.scalar(select(func.count(User.id))) == 4  # nobody was created
        assert s.scalar(select(User.role).where(User.id == world.users["rep2"])) == "rep"
        assert s.scalar(select(Job.status).where(Job.id == world.dead_job)) == "dead"


def test_admin_actions_work_for_admins(world: World) -> None:
    _as(world, "admin")
    for url, data in _admin_posts(world):
        assert post(world.c, url, data).status_code == 303, url
    with app_of(world.c).state.session_factory() as s:
        assert s.scalar(select(func.count(User.id))) == 5
        assert s.scalar(select(User.role).where(User.id == world.users["rep2"])) == "admin"
        assert s.scalar(select(Job.status).where(Job.id == world.dead_job)) == "queued"
        actions = set(s.scalars(select(AuditEvent.action)))
    assert {
        "job_retried",
        "kill_switch",
        "user_created",
        "user_updated",
        "user_unlocked",
        "password_reset",
    } <= actions


# --------------------------------------------------------------------------- CSRF


def _all_posts(w: World) -> list[tuple[str, dict[str, str]]]:
    lead = w.leads["mine"]
    return [
        (f"/dashboard/leads/{lead}/reveal", {}),
        (f"/dashboard/leads/{lead}/stage", {"stage": "qualified"}),
        (f"/dashboard/leads/{lead}/assign", {"assignee": "me"}),
        (f"/dashboard/tasks/{w.tasks['mine']}/close", {}),
        *_admin_posts(w),
        ("/logout", {}),
    ]


def test_every_state_change_needs_a_csrf_token_bound_to_the_session(world: World) -> None:
    _as(world, "admin")
    good = csrf(world.c)
    for url, data in _all_posts(world):
        assert world.c.post(url, data=data).status_code == 403, url  # none
        assert world.c.post(url, data={**data, "csrf_token": "forged"}).status_code == 403, url
        # The right token, sent cross-site (the browser marks it), is refused too.
        r = world.c.post(
            url, data={**data, "csrf_token": good}, headers={"Sec-Fetch-Site": "cross-site"}
        )
        assert r.status_code == 403, url
        r = world.c.post(url, data={**data, "csrf_token": good}, headers={"Origin": "null"})
        assert r.status_code == 403, url
    # A token issued to another session does not work in this one.
    other = make_client()
    oc = next(other)
    add_user(app_of(oc), UserRole.ADMIN)
    login(oc)
    foreign = csrf(oc)
    session_cookie = world.c.cookies.get("stl_session")
    world.c.cookies.clear()
    world.c.cookies.set("stl_session", session_cookie or "")
    world.c.cookies.set("stl_csrf", foreign)
    r = world.c.post(f"/dashboard/leads/{world.leads['mine']}/reveal", data={"csrf_token": foreign})
    assert r.status_code == 403
    # The header form works for scripts on the same origin.
    world.c.cookies.clear()
    world.c.cookies.set("stl_session", session_cookie or "")
    token = csrf(world.c)
    r = world.c.post(
        f"/dashboard/leads/{world.leads['mine']}/reveal",
        headers={"X-CSRF-Token": token, "Origin": "http://localhost"},
    )
    assert r.status_code == 200


def test_unauthenticated_posts_go_to_login(world: World) -> None:
    world.c.cookies.clear()
    for url, data in _all_posts(world)[:-1]:
        r = world.c.post(url, data=data)
        assert r.status_code == 303, url
        assert r.headers["location"].startswith("/login")
