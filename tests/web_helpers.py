"""Helpers for dashboard tests: a client per test, users per role, login, CSRF tokens."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from http.cookies import SimpleCookie
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import UserRole
from tests.conftest import PASSWORD, ROLE_EMAILS, add_user, drain, make_settings, seed_lead


def make_client(base_url: str = "http://localhost", **overrides: Any) -> Iterator[TestClient]:
    app = create_app(make_settings(**overrides))
    with TestClient(app, base_url=base_url, follow_redirects=False) as c:
        yield c


def app_of(c: TestClient) -> FastAPI:
    app: FastAPI = c.app  # type: ignore[assignment]
    return app


def login_csrf(c: TestClient) -> str:
    c.get("/login")
    token = c.cookies.get("stl_csrf")
    assert token
    return token


def login(
    c: TestClient,
    email: str = ROLE_EMAILS[UserRole.ADMIN],
    password: str = PASSWORD,
    next_path: str = "/dashboard",
) -> Any:
    return c.post(
        "/login",
        data={
            "email": email,
            "password": password,
            "csrf_token": login_csrf(c),
            "next": next_path,
        },
    )


def pass_2fa(c: TestClient, response: Any, secret: str, next_path: str = "/dashboard") -> Any:
    """Complete the two-factor step that ``response`` (a login) redirected to."""
    from app.models import utcnow
    from app.security import totp

    assert response.headers["location"].startswith("/login/2fa")
    c.get(response.headers["location"])
    return c.post(
        "/login/2fa",
        data={
            "code": totp.current_code(secret, utcnow()),
            "csrf_token": c.cookies.get("stl_csrf"),
            "next": next_path,
        },
    )


def login_as(c: TestClient, role: UserRole | str) -> int:
    """Create a user with ``role`` (if needed) and log in. Returns the user id."""
    role = UserRole(role)
    from sqlalchemy import select

    from app.models import User

    with app_of(c).state.session_factory() as s:
        existing = s.scalar(select(User.id).where(User.email == ROLE_EMAILS[role]))
    user_id = existing if existing is not None else add_user(app_of(c), role)
    r = login(c, ROLE_EMAILS[role])
    assert r.status_code == 303, r.text
    return user_id


def csrf(c: TestClient, page: str = "/dashboard") -> str:
    """A CSRF token bound to the current session (any dashboard page issues one)."""
    r = c.get(page)
    assert r.status_code in (200, 403), r.status_code
    token = c.cookies.get("stl_csrf")
    assert token
    return token


def post(
    c: TestClient,
    url: str,
    data: dict[str, Any] | None = None,
    csrf_page: str = "/dashboard",
    **kw: Any,
) -> Any:
    """POST a dashboard form with a valid CSRF token (taken from ``csrf_page``)."""
    return c.post(url, data={**(data or {}), "csrf_token": csrf(c, csrf_page)}, **kw)


def seeded(c: TestClient, n: int = 3) -> list[uuid.UUID]:
    app = app_of(c)
    ids = [seed_lead(app)]
    ids += [
        seed_lead(app, phone=f"(202) 555-01{40 + i}", email=f"lead{i}@example.com")
        for i in range(n - 1)
    ]
    drain(c)
    return ids


def set_cookie(response: Any, name: str) -> SimpleCookie:
    for header in response.headers.get_list("set-cookie"):
        if header.startswith(name + "="):
            jar: SimpleCookie = SimpleCookie()
            jar.load(header)
            return jar
    raise AssertionError(f"no {name} cookie set")


def assign(c: TestClient, lead_id: uuid.UUID, user_id: int | None) -> None:
    from app.models import Lead

    with app_of(c).state.session_factory() as s:
        lead = s.get(Lead, lead_id)
        assert lead is not None
        lead.assignee_id = user_id
        s.commit()
