"""Shared pieces of the server-rendered dashboard: templates, who is logged in, permission
checks, CSRF verification and the error pages.

Every dashboard route depends on ``require(<permission>)`` (GET) or
``require_post(<permission>)`` (state changes). The order on a POST is: logged in
(else redirect to /login) -> same-origin and a valid CSRF token bound to this session
(else 403) -> the permission (else 403). Routes on a single lead then check the object rule
(``can_on_lead``) as well.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app import __version__
from app.api.deps import SessionDep, dashboard_principal
from app.config import Settings
from app.models import UserRole
from app.security.authz import Permission, Principal, has
from app.security.csrf import (
    CSRF_COOKIE,
    CSRF_FIELD,
    CSRF_HEADER,
    check_double_submit,
    issue_token,
)
from app.security.sessions import cookie_secure
from app.services.display import display_scorer

ACCOUNT_PREFIX = "/dashboard/account"
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals["display_scorer"] = display_scorer

# Vendored Chart.js (app/web/static/vendor, MIT). The SRI hash pins the exact file.
CHARTJS_VERSION = "4.5.1"
CHARTJS_SRI = (
    "sha384-"
    "jb8JQMbMoBUzgWatfe6COACi2ljcDdZQ2Ox"  # pragma: allowlist secret
    "czGA3bGNeWe+6DChMTBJemed7ZnvJ"  # pragma: allowlist secret
)


class LoginRequired(Exception):  # noqa: N818  (control flow)
    def __init__(self, next_path: str) -> None:
        super().__init__(next_path)
        self.next_path = next_path


class MFASetupRequired(Exception):  # noqa: N818  (control flow)
    """An admin without two-factor may only use the account page until they set it up."""


def mfa_setup_redirect(request: Request, exc: Exception) -> Response:
    return RedirectResponse("/dashboard/account?setup=required", status_code=303)


class Forbidden(Exception):  # noqa: N818  (control flow)
    def __init__(self, message: str = "You do not have access to that.") -> None:
        super().__init__(message)
        self.message = message


def login_redirect(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, LoginRequired)
    return RedirectResponse(f"/login?next={quote(exc.next_path, safe='')}", status_code=303)


def forbidden_page(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, Forbidden)
    return render(request, "error.html", {"message": exc.message, "page": ""}, status_code=403)


def safe_next(value: str | None) -> str:
    """Only redirect back into the dashboard (no open redirect)."""
    if value and value.startswith("/dashboard") and "//" not in value and "\\" not in value:
        return value
    return "/dashboard"


# --------------------------------------------------------------------------- who


def current_principal(request: Request, session: SessionDep) -> Principal:
    found = dashboard_principal(request, session)
    if found is None:
        path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        raise LoginRequired(path if request.method == "GET" else "/dashboard")
    request.state.principal = found
    settings: Settings = request.app.state.settings
    if (
        settings.admin_2fa_required
        and found.role is UserRole.ADMIN
        and not found.mfa_enabled
        and not request.url.path.startswith(ACCOUNT_PREFIX)
    ):
        raise MFASetupRequired()
    return found


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]


def require(permission: Permission) -> Callable[..., Principal]:
    def dependency(principal: CurrentPrincipal) -> Principal:
        if not has(principal, permission):
            raise Forbidden()
        return principal

    return dependency


def _same_origin(request: Request) -> bool:
    """Reject cross-site POSTs by Sec-Fetch-Site / Origin when the browser sends them.

    Hosts are compared (not schemes or ports), so a TLS-terminating proxy in front does not
    break it. Requests without these headers still need the CSRF token.
    """
    if request.headers.get("sec-fetch-site") in ("cross-site", "same-site"):
        return False
    origin = request.headers.get("origin")
    if origin is None:
        return True
    return origin != "null" and urlsplit(origin).hostname == request.url.hostname


async def csrf_ok(request: Request, binding: str) -> bool:
    settings: Settings = request.app.state.settings
    if not _same_origin(request):
        return False
    form = await request.form()
    token = str(form.get(CSRF_FIELD) or request.headers.get(CSRF_HEADER) or "")
    return check_double_submit(
        settings.secret_key_bytes, request.cookies.get(CSRF_COOKIE), token, binding=binding
    )


def require_post(permission: Permission) -> Callable[..., Awaitable[Principal]]:
    async def dependency(request: Request, principal: CurrentPrincipal) -> Principal:
        if not await csrf_ok(request, principal.session_hash or ""):
            raise HTTPException(status_code=403, detail="invalid form token")
        if not has(principal, permission):
            raise Forbidden()
        return principal

    return dependency


# --------------------------------------------------------------------------- rendering


def _local(value: datetime | None, zone: ZoneInfo) -> str:
    return value.astimezone(zone).strftime("%b %d, %H:%M") if value else ""


def duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def render(
    request: Request,
    template: str,
    context: dict[str, Any],
    *,
    status_code: int = 200,
    principal: Principal | None = None,
) -> HTMLResponse:
    settings: Settings = request.app.state.settings
    principal = principal or getattr(request.state, "principal", None)
    binding = principal.session_hash or "" if principal is not None else ""
    csrf = issue_token(settings.secret_key_bytes, binding=binding)
    zone = ZoneInfo(settings.business_timezone)
    base = {
        "company": settings.company_name,
        "version": __version__,
        "demo_mode": settings.demo_mode,
        "real_email": settings.real_email_configured,
        "user": principal,
        "can": (lambda p: principal is not None and has(principal, Permission(p))),
        "csrf_field": CSRF_FIELD,
        "csrf_token": csrf,
        "tz": settings.business_timezone,
        "local": lambda v: _local(v, zone),
        "duration": duration,
        "chartjs_version": CHARTJS_VERSION,
        "chartjs_sri": CHARTJS_SRI,
    }
    response = templates.TemplateResponse(
        request, template, {**base, **context}, status_code=status_code
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        httponly=True,
        samesite="strict",
        secure=cookie_secure(request.url.hostname),
        max_age=2 * 60 * 60,
    )
    return response
