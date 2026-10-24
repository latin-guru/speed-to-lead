"""Interview mode pages.

- ``/dashboard/interview`` (admins and reps): create a single-use invite link, see active,
  used and expired invites, revoke one. Every change is audited.
- ``/try/<token>`` (anyone holding a valid link): a short form; on submit the invite is
  used up, a lead is created through the real pipeline (source ``interview_demo``), and the
  page turns into a live panel of what happened to it (``/try/<token>/status``, polled).

The only thing an invite changes is that its lead's one email acknowledgment may go to the
real email sender (see ``app.interview.routing``). SMS stays mock, always.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import ValidationError

from app.api.deps import SessionDep, client_ip
from app.config import Settings
from app.interview import INTERVIEW_SOURCE, invites
from app.interview.mailer import mask_email
from app.interview.panel import panel
from app.interview.routing import real_sent_since
from app.models import InterviewInvite, utcnow
from app.schemas import LeadIn
from app.security.authz import Permission, Principal
from app.security.sessions import cookie_secure
from app.services import audit
from app.services.leads import create_lead
from app.web.common import csrf_ok, render, require, require_post
from app.web.routes import HONEYPOT_FIELD, _error_messages

log = logging.getLogger(__name__)
router = APIRouter(tags=["interview"], include_in_schema=False)

VIEW_COOKIE = "stl_try"
FIRST_NAME_MAX = 40
MESSAGE_MAX = 500
CONSENT_TEXT = (
    "I agree to receive one demo email at this address. This is a one-time demo; no marketing."
)
CLOSED_MESSAGE = "This demo link is not valid, has expired, or was already used."
NOINDEX = {"X-Robots-Tag": "noindex, nofollow"}


def _base_url(request: Request) -> str:
    settings: Settings = request.app.state.settings
    scheme = request.url.scheme
    if settings.trust_forwarded_for:
        proto = request.headers.get("x-forwarded-proto", "").split(",")[-1].strip().lower()
        scheme = proto if proto in ("http", "https") else scheme
    return f"{scheme}://{request.url.netloc}"


# --------------------------------------------------------------------------- dashboard


def _dashboard(
    request: Request,
    session: SessionDep,
    *,
    new_link: str | None = None,
    new_label: str | None = None,
    done: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    settings: Settings = request.app.state.settings
    now = utcnow()
    rows = [
        {
            "id": inv.id,
            "label": inv.label,
            "status": inv.status(now),
            "created_by": inv.created_by,
            "created_at": inv.created_at,
            "expires_at": inv.expires_at,
            "used_at": inv.used_at,
            "email_result": inv.email_result,
        }
        for inv in invites.list_invites(session)
    ]
    return render(
        request,
        "interview.html",
        {
            "page": "interview",
            "invites": rows,
            "new_link": new_link,
            "new_label": new_label,
            "done": done,
            "ttl_minutes": settings.invite_ttl_minutes,
            "real_email": settings.real_email_configured,
            "backend": settings.email_backend,
            "per_hour": settings.interview_email_max_per_hour,
            "per_day": settings.interview_email_max_per_day,
            "sent_hour": real_sent_since(session, now - timedelta(hours=1)),
            "sent_day": real_sent_since(session, now - timedelta(days=1)),
        },
        status_code=status_code,
    )


@router.get("/dashboard/interview", response_class=HTMLResponse)
def interview_page(
    request: Request,
    session: SessionDep,
    principal: Annotated[Principal, Depends(require(Permission.MANAGE_INVITES))],
    done: str | None = None,
) -> HTMLResponse:
    return _dashboard(request, session, done="revoked" if done == "revoked" else None)


@router.post("/dashboard/interview", response_class=HTMLResponse)
async def invite_create(
    request: Request,
    session: SessionDep,
    principal: Annotated[Principal, Depends(require_post(Permission.MANAGE_INVITES))],
) -> HTMLResponse:
    settings: Settings = request.app.state.settings
    form = await request.form()
    created = invites.create_invite(
        session,
        created_by=principal.audit_actor,
        label=str(form.get("label") or "")[:200],
        ttl=timedelta(minutes=settings.invite_ttl_minutes),
        now=utcnow(),
    )
    audit.record_for(
        session,
        principal,
        "invite_created",
        request=request,
        target_type="invite",
        target_id=created.invite.id,
        summary={"label": created.invite.label, "ttl_minutes": settings.invite_ttl_minutes},
    )
    session.commit()
    # The plain link exists only in this response: the database keeps a hash of it.
    link = f"{_base_url(request)}/try/{created.token}"
    return _dashboard(
        request, session, new_link=link, new_label=created.invite.label, status_code=201
    )


@router.post("/dashboard/interview/{invite_id}/revoke")
def invite_revoke(
    invite_id: int,
    request: Request,
    session: SessionDep,
    principal: Annotated[Principal, Depends(require_post(Permission.MANAGE_INVITES))],
) -> Response:
    invite = session.get(InterviewInvite, invite_id)
    changed = invite is not None and invites.revoke(session, invite, now=utcnow())
    audit.record_for(
        session,
        principal,
        "invite_revoked",
        request=request,
        target_type="invite",
        target_id=invite_id,
        outcome="ok" if changed else "failed",
        summary={"label": invite.label if invite is not None else None},
    )
    session.commit()
    return RedirectResponse("/dashboard/interview?done=revoked", status_code=303)


# --------------------------------------------------------------------------- /try


def _try_page(
    request: Request,
    *,
    token: str,
    mode: str,
    errors: list[str] | None = None,
    values: dict[str, str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    response = render(
        request,
        "try.html",
        {
            "page": "try",
            "mode": mode,  # form | panel | closed
            "token": token,
            "errors": errors or [],
            "values": values or {},
            "honeypot": HONEYPOT_FIELD,
            "consent_text": CONSENT_TEXT,
            "closed_message": CLOSED_MESSAGE,
            "first_name_max": FIRST_NAME_MAX,
            "message_max": MESSAGE_MAX,
        },
        status_code=status_code,
    )
    response.headers.update(NOINDEX)
    return response


def _closed(request: Request, token: str) -> HTMLResponse:
    return _try_page(request, token="", mode="closed", status_code=404)


@router.get("/try/{token}", response_class=HTMLResponse)
def try_page(token: str, request: Request, session: SessionDep) -> HTMLResponse:
    invite = invites.find_by_token(session, token)
    if invite is None:
        return _closed(request, token)
    if invites.viewer_matches(invite, request.cookies.get(VIEW_COOKIE)):
        return _try_page(request, token=token, mode="panel")
    if invite.status(utcnow()) != "active":
        return _closed(request, token)
    return _try_page(request, token=token, mode="form")


def _form_values(form: Any) -> dict[str, str]:
    return {
        "first_name": str(form.get("first_name") or "")[: FIRST_NAME_MAX * 2],
        "email": str(form.get("email") or "")[:320],
        "message": str(form.get("message") or "")[: MESSAGE_MAX * 2],
        "consent": "yes" if form.get("consent") == "yes" else "",
    }


def _validate(values: dict[str, str]) -> tuple[LeadIn | None, list[str]]:
    errors: list[str] = []
    first = values["first_name"].strip()
    email = values["email"].strip()
    message = values["message"].strip()
    if not first:
        errors.append("Enter your first name.")
    elif len(first) > FIRST_NAME_MAX:
        errors.append(f"First name: at most {FIRST_NAME_MAX} characters.")
    if not email:
        errors.append("Enter your email address.")
    elif len(email) > 254:
        errors.append("Email: at most 254 characters.")
    if len(message) > MESSAGE_MAX:
        errors.append(f"Message: at most {MESSAGE_MAX} characters.")
    if values["consent"] != "yes":
        errors.append("Tick the box to agree to receive the one demo email.")
    if errors:
        return None, errors
    try:
        data = LeadIn.model_validate(
            {
                "name": first,
                "email": email,
                "message": message or None,
                "source": INTERVIEW_SOURCE,
            }
        )
    except ValidationError as exc:
        return None, _error_messages(exc)
    return data, []


@router.post("/try/{token}", response_class=HTMLResponse)
async def try_submit(token: str, request: Request, session: SessionDep) -> Response:
    settings: Settings = request.app.state.settings
    form = await request.form()
    values = _form_values(form)
    if not request.app.state.try_limiter.hit(f"try:{client_ip(request)}").allowed:
        return _try_page(
            request,
            token=token,
            mode="form",
            errors=["Too many attempts from this network. Try again later."],
            values=values,
            status_code=429,
        )
    if not await csrf_ok(request, ""):
        return _try_page(
            request,
            token=token,
            mode="form",
            errors=["Your session expired. Please try again."],
            values=values,
            status_code=403,
        )
    invite = invites.find_by_token(session, token)
    now = utcnow()
    if invite is None or invite.status(now) != "active":
        return _closed(request, token)
    if str(form.get(HONEYPOT_FIELD) or "").strip():
        log.info("honeypot_triggered", extra={"source": INTERVIEW_SOURCE})
        return RedirectResponse("/thanks", status_code=303)  # the invite stays unused
    data, errors = _validate(values)
    if data is None:
        return _try_page(
            request, token=token, mode="form", errors=errors, values=values, status_code=422
        )
    if not invites.claim(session, invite, now=now):
        session.rollback()
        return _closed(request, token)
    lead = create_lead(
        session,
        data,
        actor="interview_link",
        detail={"channel": INTERVIEW_SOURCE, "invite_id": invite.id},
        now=now,
    )
    request.app.state.intake.on_lead_created(session, lead, now=now)
    view_token, view_hash = invites.new_viewer_token()
    invite.lead_id = lead.id
    invite.viewer_hash = view_hash
    audit.record(
        session,
        actor="interview-link",
        role=None,
        action="invite_used",
        target_type="invite",
        target_id=invite.id,
        summary={"label": invite.label, "lead_id": str(lead.id)},
        request=request,
        now=now,
    )
    session.commit()
    assert data.email is not None  # required above
    log.info(
        "invite_used",
        extra={"invite_id": invite.id, "lead_id": str(lead.id), "to": mask_email(data.email)},
    )
    response = RedirectResponse(f"/try/{token}", status_code=303)
    response.set_cookie(
        VIEW_COOKIE,
        view_token,
        httponly=True,
        samesite="strict",
        secure=cookie_secure(request.url.hostname),
        max_age=settings.invite_ttl_minutes * 60,
        path=f"/try/{token}",
    )
    return response


@router.get("/try/{token}/status")
def try_status(token: str, request: Request, session: SessionDep) -> JSONResponse:
    invite = invites.find_by_token(session, token)
    if invite is None or not invites.viewer_matches(invite, request.cookies.get(VIEW_COOKIE)):
        return JSONResponse({"detail": "not found"}, status_code=404, headers=NOINDEX)
    data = panel(session, invite, crm_enabled=request.app.state.intake.crm_enabled)
    return JSONResponse(data, headers=NOINDEX)
