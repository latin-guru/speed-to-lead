"""Dashboard login and logout (email + password; server-side sessions).

Failures all get the same message and status, whatever the reason (unknown email, wrong
password, locked or inactive account). Login is rate-limited per client IP on top of the
per-account lockout. See ``app.services.users`` and ``app.security.sessions``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.api.deps import (
    SessionDep,
    client_ip,
    dashboard_principal,
    login_rate_limit,
    session_cookie,
    session_policy,
)
from app.config import Settings
from app.models import User, utcnow
from app.security import totp
from app.security.sessions import (
    MFA_COOKIE,
    MFA_MAX_AGE_SECONDS,
    cookie_name,
    cookie_secure,
    find_session,
    issue_pending,
    read_pending,
    revoke,
    start_session,
)
from app.services import audit, mfa
from app.services.users import AuthOutcome, LoginPolicy, authenticate
from app.web.common import csrf_ok, render, safe_next

log = logging.getLogger(__name__)
router = APIRouter(tags=["dashboard"], include_in_schema=False)

GENERIC_ERROR = "That email and password did not work. Check them, or try again in a few minutes."
CODE_ERROR = "That code did not work. Use the newest code, or try again in a few minutes."
DEMO_ADMIN_EMAIL = "admin@example.com"


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

    if user.mfa_enabled:
        session.commit()  # a rehashed password, if any
        response = RedirectResponse(f"/login/2fa?next={quote(next_path, safe='')}", status_code=303)
        response.set_cookie(
            MFA_COOKIE,
            issue_pending(settings.secret_key_bytes, user, now=now),
            max_age=MFA_MAX_AGE_SECONDS,
            httponly=True,
            samesite="strict",
            secure=cookie_secure(request.url.hostname),
            path="/login",
        )
        return response
    return _finish_login(request, session, user, next_path=next_path, now=now)


def _finish_login(
    request: Request,
    session: Session,
    user: User,
    *,
    next_path: str,
    now: datetime,
    mfa_method: str | None = None,
) -> Response:
    settings: Settings = request.app.state.settings
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
        summary={"mfa": mfa_method} if mfa_method else None,
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
    if mfa_method:
        response.delete_cookie(
            MFA_COOKIE, path="/login", httponly=True, samesite="strict", secure=secure
        )
    log.info("dashboard_login", extra={"role": user.role, "mfa": mfa_method})
    return response


# --------------------------------------------------------------------------- two-factor step


def _demo_code(settings: Settings, user: User, now: datetime) -> str | None:
    """In demo mode the demo admin uses a public secret; show its current code."""
    if not settings.demo_mode or user.email != DEMO_ADMIN_EMAIL:
        return None
    secret = mfa.active_secret(user)
    return totp.current_code(secret, now) if secret == totp.demo_secret() else None


def _code_page(
    request: Request,
    user: User,
    *,
    next_path: str,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    settings: Settings = request.app.state.settings
    return render(
        request,
        "login_2fa.html",
        {
            "next": next_path,
            "error": error,
            "page": "login",
            "demo_code": _demo_code(settings, user, utcnow()),
        },
        status_code=status_code,
    )


@router.get("/login/2fa", response_class=HTMLResponse)
def code_page(request: Request, session: SessionDep, next: str | None = None) -> Response:
    settings: Settings = request.app.state.settings
    user = read_pending(
        session, settings.secret_key_bytes, request.cookies.get(MFA_COOKIE), now=utcnow()
    )
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return _code_page(request, user, next_path=safe_next(next))


@router.post("/login/2fa", response_class=HTMLResponse, dependencies=[Depends(login_rate_limit)])
async def code_submit(request: Request, session: SessionDep) -> Response:
    settings: Settings = request.app.state.settings
    form = await request.form()
    next_path = safe_next(str(form.get("next") or ""))
    now = utcnow()
    user = read_pending(
        session, settings.secret_key_bytes, request.cookies.get(MFA_COOKIE), now=now
    )
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if not await csrf_ok(request, ""):
        return _code_page(
            request,
            user,
            next_path=next_path,
            error="Your form expired. Try again.",
            status_code=403,
        )
    code = str(form.get("code") or "")[:64]
    result = mfa.verify_second_factor(session, user, code, now=now, policy=login_policy(settings))
    if not result.ok:
        audit.record(
            session,
            actor=user.email,
            role=user.role,
            action="mfa_verify",
            outcome="failed",
            summary={"locked": result.locked},
            request=request,
            now=now,
        )
        session.commit()
        return _code_page(request, user, next_path=next_path, error=CODE_ERROR, status_code=401)
    assert result.method is not None
    audit.record(
        session,
        actor=user.email,
        role=user.role,
        action="mfa_recovery_used" if result.method is mfa.Method.RECOVERY else "mfa_verify",
        summary={"method": result.method.value, "codes_left": result.remaining_codes},
        request=request,
        now=now,
    )
    return _finish_login(
        request, session, user, next_path=next_path, now=now, mfa_method=result.method.value
    )


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
