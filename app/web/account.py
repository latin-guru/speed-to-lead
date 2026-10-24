"""The signed-in user's own account page: two-factor set-up, recovery codes, turning it off.

Admins must set up two-factor before they can use the rest of the dashboard
(``ADMIN_2FA_REQUIRED``); reps may; viewers are not offered it. The QR code is drawn on the
server and embedded as a ``data:`` URI, so the secret never leaves this app.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.api.deps import SessionDep
from app.config import Settings
from app.models import User, utcnow
from app.security import totp
from app.security.authz import Permission, Principal
from app.services import audit, mfa
from app.web.common import render, require, require_post

router = APIRouter(prefix="/dashboard/account", tags=["dashboard"], include_in_schema=False)

Reader = Annotated[Principal, Depends(require(Permission.VIEW_DASHBOARD))]
Writer = Annotated[Principal, Depends(require_post(Permission.VIEW_DASHBOARD))]

ERRORS = {
    "bad_code": "That code did not match. Use the newest code from your app.",
    "no_pending": "Start the set-up again.",
    "not_allowed": "Two-factor is not available for your role.",
    "required": "Two-factor is required for admins, so it cannot be turned off.",
    "demo": "The shared demo accounts cannot change their two-factor settings.",
}


def _user(session: Session, principal: Principal) -> User:
    assert principal.user_id is not None  # dashboard sessions always have a user
    user = session.get(User, principal.user_id)
    assert user is not None
    return user


def _page(
    request: Request,
    session: Session,
    user: User,
    *,
    error: str | None = None,
    enrolling: dict[str, str] | None = None,
    codes: list[str] | None = None,
    status_code: int = 200,
    setup_required: bool = False,
) -> HTMLResponse:
    settings: Settings = request.app.state.settings
    context: dict[str, Any] = {
        "page": "account",
        "account": user,
        "can_enroll": mfa.can_enroll(user),
        "required": mfa.required_for(user, admin_required=settings.admin_2fa_required),
        "setup_required": setup_required and not user.mfa_enabled,
        "remaining": mfa.remaining_recovery_codes(session, user) if user.mfa_enabled else 0,
        "error": ERRORS.get(error or ""),
        "enrolling": enrolling,
        "codes": codes,
        "demo_locked": settings.demo_mode,
    }
    return render(request, "account.html", context, status_code=status_code)


@router.get("", response_class=HTMLResponse)
def account_page(
    request: Request, session: SessionDep, principal: Reader, setup: str | None = None
) -> HTMLResponse:
    return _page(request, session, _user(session, principal), setup_required=setup == "required")


def _demo_locked(request: Request) -> bool:
    settings: Settings = request.app.state.settings
    return settings.demo_mode


@router.post("/2fa/start", response_class=HTMLResponse)
def start(request: Request, session: SessionDep, principal: Writer) -> HTMLResponse:
    user = _user(session, principal)
    if _demo_locked(request):
        return _page(request, session, user, error="demo", status_code=403)
    settings: Settings = request.app.state.settings
    try:
        secret = mfa.begin_enrollment(user)
    except mfa.MFAError as exc:
        return _page(request, session, user, error=exc.code, status_code=403)
    session.commit()
    uri = totp.provisioning_uri(secret, account=user.email, issuer=settings.totp_issuer)
    enrolling = {"qr": totp.qr_svg_data_uri(uri), "secret": secret}
    return _page(request, session, user, enrolling=enrolling)


@router.post("/2fa/confirm", response_class=HTMLResponse)
async def confirm(request: Request, session: SessionDep, principal: Writer) -> HTMLResponse:
    user = _user(session, principal)
    if _demo_locked(request):
        return _page(request, session, user, error="demo", status_code=403)
    form = await request.form()
    try:
        codes = mfa.confirm_enrollment(
            session, user, str(form.get("code") or "")[:16], now=utcnow()
        )
    except mfa.MFAError as exc:
        audit.record_for(session, principal, "mfa_enrolled", request=request, outcome="failed")
        session.commit()
        return _page(request, session, user, error=exc.code, status_code=422)
    audit.record_for(
        session, principal, "mfa_enrolled", request=request, target_type="user", target_id=user.id
    )
    session.commit()
    return _page(request, session, user, codes=codes)


async def _code_ok(request: Request, session: Session, user: User) -> bool:
    form = await request.form()
    return mfa.check_current_code(user, str(form.get("code") or "")[:16], now=utcnow())


@router.post("/2fa/recovery-codes", response_class=HTMLResponse)
async def new_codes(request: Request, session: SessionDep, principal: Writer) -> HTMLResponse:
    user = _user(session, principal)
    if _demo_locked(request):
        return _page(request, session, user, error="demo", status_code=403)
    if not user.mfa_enabled or not await _code_ok(request, session, user):
        audit.record_for(
            session, principal, "mfa_recovery_regenerated", request=request, outcome="failed"
        )
        session.commit()
        return _page(request, session, user, error="bad_code", status_code=422)
    codes = mfa.regenerate_recovery_codes(session, user, now=utcnow())
    audit.record_for(
        session,
        principal,
        "mfa_recovery_regenerated",
        request=request,
        target_type="user",
        target_id=user.id,
    )
    session.commit()
    return _page(request, session, user, codes=codes)


@router.post("/2fa/disable")
async def turn_off(request: Request, session: SessionDep, principal: Writer) -> Response:
    settings: Settings = request.app.state.settings
    user = _user(session, principal)
    if _demo_locked(request):
        return _page(request, session, user, error="demo", status_code=403)
    if mfa.required_for(user, admin_required=settings.admin_2fa_required):
        return _page(request, session, user, error="required", status_code=403)
    if not user.mfa_enabled or not await _code_ok(request, session, user):
        return _page(request, session, user, error="bad_code", status_code=422)
    mfa.disable(session, user, now=utcnow())  # also ends this session: log in again
    audit.record_for(
        session, principal, "mfa_disabled", request=request, target_type="user", target_id=user.id
    )
    session.commit()
    return RedirectResponse("/login", status_code=303)
