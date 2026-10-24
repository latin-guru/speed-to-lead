"""Dashboard login and logout (email + password; server-side sessions).

Failures all get the same message and status, whatever the reason (unknown email, wrong
password, locked or inactive account). Login is rate-limited per client IP on top of the
per-account lockout. See ``app.services.users`` and ``app.security.sessions``.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.api.deps import (
    SessionDep,
    client_ip,
    dashboard_principal,
    login_rate_limit,
    session_cookie,
    session_policy,
)
from app.config import Settings
from app.models import utcnow
from app.security.sessions import (
    cookie_name,
    cookie_secure,
    find_session,
    revoke,
    start_session,
)
from app.services import audit
from app.services.users import AuthOutcome, LoginPolicy, authenticate
from app.web.common import csrf_ok, render, safe_next

log = logging.getLogger(__name__)
router = APIRouter(tags=["dashboard"], include_in_schema=False)

GENERIC_ERROR = "That email and password did not work. Check them, or try again in a few minutes."


def login_policy(settings: Settings) -> LoginPolicy:
    return LoginPolicy(
        max_failures=settings.login_max_failures,
        lockout_base=timedelta(seconds=settings.login_lockout_base_seconds),
        lockout_max=timedelta(seconds=settings.login_lockout_max_seconds),
    )


def _login_page(
    request: Request,
    *,
    next_path: str,
    error: str | None = None,
    email: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    settings: Settings = request.app.state.settings
    demo_accounts: list[tuple[str, str]] = []
    demo_password = None
    if settings.demo_mode and settings.demo_password is not None:
        demo_accounts = [(f"{role}@example.com", role) for role in settings.demo_role_list]
        demo_password = settings.demo_password.get_secret_value()
    return render(
        request,
        "login.html",
        {
            "next": next_path,
            "error": error,
            "email": email,
            "demo_accounts": demo_accounts,
            "demo_password": demo_password,
            "page": "login",
        },
        status_code=status_code,
    )


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, session: SessionDep, next: str | None = None) -> Response:
    if dashboard_principal(request, session) is not None:
        return RedirectResponse(safe_next(next), status_code=303)
    return _login_page(request, next_path=safe_next(next))


@router.post("/login", response_class=HTMLResponse, dependencies=[Depends(login_rate_limit)])
async def login_submit(request: Request, session: SessionDep) -> Response:
    settings: Settings = request.app.state.settings
    form = await request.form()
    next_path = safe_next(str(form.get("next") or ""))
    email = str(form.get("email") or "").strip()[:254]
    if not await csrf_ok(request, ""):
        return _login_page(
            request,
            next_path=next_path,
            error="Your session expired. Try again.",
            email=email,
            status_code=403,
        )
    password = str(form.get("password") or "")[:1024]
    now = utcnow()
    result = authenticate(session, email, password, now=now, policy=login_policy(settings))
    user = result.user
    if not result.ok or user is None:
        audit.record(
            session,
            actor=user.email if user is not None else "unknown",
            role=user.role if user is not None else None,
            action="login",
            outcome="failed",
            summary={
                "reason": result.outcome.value,
                "locked": result.locked_now or result.outcome is AuthOutcome.LOCKED,
            },
            request=request,
            now=now,
        )
        session.commit()
        log.warning("dashboard_login_failed", extra={"reason": result.outcome.value})
        return _login_page(
            request, next_path=next_path, error=GENERIC_ERROR, email=email, status_code=401
        )

    # Rotation: whatever session this browser had ends now; the login gets a fresh one.
    previous = find_session(session, settings.secret_key_bytes, session_cookie(request))
    if previous is not None:
        revoke(session, previous, now=now)
    secure = cookie_secure(request.url.hostname)
    policy = session_policy(settings)
    cookie, row = start_session(
        session,
        settings.secret_key_bytes,
        user,
        now=now,
        policy=policy,
        ip_hash=audit.ip_hash(settings.secret_key_bytes, client_ip(request)),
        user_agent=request.headers.get("user-agent"),
    )
    audit.record(
        session,
        actor=user.email,
        role=user.role,
        action="login",
        target_type="session",
        target_id=row.id,
        request=request,
        now=now,
    )
    session.commit()
    response = RedirectResponse(next_path, status_code=303)
    response.set_cookie(
        cookie_name(secure),
        cookie,
        max_age=int(policy.absolute.total_seconds()),
        httponly=True,
        samesite="strict",
        secure=secure,
        path="/",
    )
    log.info("dashboard_login", extra={"role": user.role})
    return response


@router.post("/logout")
async def logout(request: Request, session: SessionDep) -> Response:
    settings: Settings = request.app.state.settings
    secure = cookie_secure(request.url.hostname)
    response = RedirectResponse("/login", status_code=303)
    principal = dashboard_principal(request, session)
    if principal is not None:
        if not await csrf_ok(request, principal.session_hash or ""):
            return render(
                request,
                "error.html",
                {"message": "Your form expired. Go back and try again.", "page": ""},
                status_code=403,
                principal=principal,
            )
        row = find_session(session, settings.secret_key_bytes, session_cookie(request))
        assert row is not None  # dashboard_principal just loaded it
        revoke(session, row, now=utcnow())
        audit.record_for(
            session, principal, "logout", request=request, target_type="session", target_id=row.id
        )
        session.commit()
    response.delete_cookie(
        cookie_name(secure), path="/", httponly=True, samesite="strict", secure=secure
    )
    return response
