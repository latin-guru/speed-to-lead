"""Server-rendered operator dashboard at ``/dashboard`` (Jinja + a little vanilla JS).

Access is a temporary login: the operator token (``ADMIN_TOKEN``), or the demo token in
demo mode, is exchanged at ``/login`` for a signed session cookie (see
``app.security.session``). Real users and roles replace this later.

Every page sends a strict Content-Security-Policy (scripts and styles from this origin
only, no inline script), and contact details are masked unless an operator reveals them,
which is logged on the lead's timeline.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import __version__
from app.api.deps import SessionDep, dashboard_session, public_rate_limit, session_tokens
from app.api.metrics import build_kpis
from app.config import Settings
from app.logging import mask_pii
from app.models import Job, Lead, LeadStage, OutboxMessage, Task, utcnow
from app.security.csrf import CSRF_COOKIE, CSRF_FIELD, check_double_submit, issue_token
from app.security.session import (
    SESSION_COOKIE,
    DashboardSession,
    cookie_secure,
    issue_session,
    match_token,
)
from app.services import metrics as m
from app.services.pipeline import ALLOWED_TRANSITIONS, HUMAN_PREFIX, TransitionError, move_stage
from app.services.timeline import add_activity

log = logging.getLogger(__name__)
router = APIRouter(tags=["dashboard"], include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

PAGE_SIZE = 25
# Vendored Chart.js (app/web/static/vendor, MIT). The SRI hash pins the exact file.
CHARTJS_VERSION = "4.5.1"
CHARTJS_SRI = (
    "sha384-"
    "jb8JQMbMoBUzgWatfe6COACi2ljcDdZQ2Ox"  # pragma: allowlist secret
    "czGA3bGNeWe+6DChMTBJemed7ZnvJ"  # pragma: allowlist secret
)
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)
STAGE_ERRORS = {
    "invalid_transition": "That stage change is not allowed from the current stage.",
    "same_stage": "The lead is already in that stage.",
    "reason_required": "A reason is required for that change.",
    "human_only": "Only a person can change a stage.",
}


class LoginRequired(Exception):  # noqa: N818  (control flow)
    def __init__(self, next_path: str) -> None:
        super().__init__(next_path)
        self.next_path = next_path


def require_session(request: Request) -> DashboardSession:
    found = dashboard_session(request)
    if found is None:
        path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        raise LoginRequired(path)
    return found


SessionUser = Annotated[DashboardSession, Depends(require_session)]


def login_redirect(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, LoginRequired)
    return RedirectResponse(f"/login?next={quote(exc.next_path, safe='')}", status_code=303)


def _safe_next(value: str | None) -> str:
    """Only redirect back into the dashboard (no open redirect)."""
    if value and value.startswith("/dashboard") and "//" not in value and "\\" not in value:
        return value
    return "/dashboard"


# --------------------------------------------------------------------------- rendering


def _zone(settings: Settings) -> ZoneInfo:
    return ZoneInfo(settings.business_timezone)


def _local(value: datetime | None, zone: ZoneInfo) -> str:
    return value.astimezone(zone).strftime("%b %d, %H:%M") if value else ""


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def _render(
    request: Request,
    template: str,
    context: dict[str, Any],
    *,
    status_code: int = 200,
    user: DashboardSession | None = None,
) -> HTMLResponse:
    settings: Settings = request.app.state.settings
    csrf = issue_token(settings.secret_key_bytes)
    zone = _zone(settings)
    base = {
        "company": settings.company_name,
        "version": __version__,
        "demo_mode": settings.demo_mode,
        "user": user,
        "csrf_field": CSRF_FIELD,
        "csrf_token": csrf,
        "tz": settings.business_timezone,
        "local": lambda v: _local(v, zone),
        "duration": _duration,
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
    _harden(response)
    return response


def _harden(response: Response) -> None:
    response.headers["Content-Security-Policy"] = CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Cache-Control"] = "no-store"


def _csrf_ok(request: Request, form_token: object) -> bool:
    settings: Settings = request.app.state.settings
    return check_double_submit(
        settings.secret_key_bytes, request.cookies.get(CSRF_COOKIE), str(form_token or "")
    )


# --------------------------------------------------------------------------- login


def _login_page(
    request: Request, *, next_path: str, error: str | None = None, status_code: int = 200
) -> HTMLResponse:
    settings: Settings = request.app.state.settings
    demo_token = session_tokens(settings)["demo"]
    return _render(
        request,
        "login.html",
        {
            "next": next_path,
            "error": error,
            "configured": any(session_tokens(settings).values()),
            "demo_token": demo_token,
        },
        status_code=status_code,
    )


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str | None = None) -> Response:
    if dashboard_session(request) is not None:
        return RedirectResponse(_safe_next(next), status_code=303)
    return _login_page(request, next_path=_safe_next(next))


@router.post("/login", response_class=HTMLResponse, dependencies=[Depends(public_rate_limit)])
async def login_submit(request: Request) -> Response:
    settings: Settings = request.app.state.settings
    form = await request.form()
    next_path = _safe_next(str(form.get("next") or ""))
    if not _csrf_ok(request, form.get(CSRF_FIELD)):
        return _login_page(
            request, next_path=next_path, error="Your session expired. Try again.", status_code=403
        )
    tokens = session_tokens(settings)
    role = match_token(str(form.get("token") or ""), tokens)
    if role is None:
        log.warning("dashboard_login_failed")
        return _login_page(
            request, next_path=next_path, error="That token is not valid.", status_code=401
        )
    token = tokens[role]
    assert token is not None
    ttl = settings.session_ttl_hours * 3600
    cookie = issue_session(settings.secret_key_bytes, role, token, now=time.time(), ttl_seconds=ttl)
    response = RedirectResponse(next_path, status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        cookie,
        max_age=int(ttl),
        httponly=True,
        samesite="strict",
        secure=cookie_secure(request.url.hostname),
        path="/",
    )
    log.info("dashboard_login", extra={"role": role})
    return response


@router.post("/logout")
async def logout(request: Request) -> Response:
    form = await request.form()
    if not _csrf_ok(request, form.get(CSRF_FIELD)):
        raise HTTPException(status_code=403, detail="invalid form token")
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        httponly=True,
        samesite="strict",
        secure=cookie_secure(request.url.hostname),
    )
    return response


# --------------------------------------------------------------------------- pages


@router.get("/dashboard", response_class=HTMLResponse)
def overview(request: Request, session: SessionDep, user: SessionUser) -> HTMLResponse:
    kpis = build_kpis(session, utcnow())  # first paint; the page then polls the JSON API
    return _render(request, "dashboard.html", {"kpis": kpis, "page": "overview"}, user=user)


def _int_or_none(value: str | None, low: int, high: int) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        return max(low, min(high, int(value)))
    except ValueError:
        return None


@router.get("/dashboard/leads", response_class=HTMLResponse)
def lead_list(
    request: Request,
    session: SessionDep,
    user: SessionUser,
    stage: str | None = None,
    source: str | None = None,
    min_score: str | None = None,
    max_score: str | None = None,
    page: int = Query(default=1, ge=1, le=10_000),
) -> HTMLResponse:
    stage = stage if stage in {s.value for s in LeadStage} else None
    sources = m.known_sources(session)
    source = source if source in sources else None
    filters = m.LeadFilter(
        stage=stage,
        source=source,
        min_score=_int_or_none(min_score, 0, 100),
        max_score=_int_or_none(max_score, 0, 100),
    )
    leads, total = m.list_leads(session, filters, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
    query = {k: v for k, v in vars(filters).items() if v is not None}
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    return _render(
        request,
        "leads.html",
        {
            "page": "leads",
            "leads": [_lead_row(lead) for lead in leads],
            "total": total,
            "filters": filters,
            "stages": [s.value for s in LeadStage],
            "sources": sources,
            "page_no": page,
            "pages": pages,
            "prev_url": f"?{urlencode({**query, 'page': page - 1})}" if page > 1 else None,
            "next_url": f"?{urlencode({**query, 'page': page + 1})}" if page < pages else None,
        },
        user=user,
    )


def _response_seconds(lead: Lead) -> float | None:
    if lead.first_response_at is None:
        return None
    return (lead.first_response_at - lead.created_at).total_seconds()


def _lead_row(lead: Lead) -> dict[str, Any]:
    return {
        "id": lead.id,
        "name": lead.name,
        "source": lead.source,
        "stage": lead.stage,
        "score": lead.score,
        "next_step": lead.next_step,
        "created_at": lead.created_at,
        "response_seconds": _response_seconds(lead),
        "opted_out": lead.opted_out_at is not None,
    }


def _mask_contact(value: str | None) -> str:
    return mask_pii(value) if value else ""


def _get_lead(session: Session, lead_id: uuid.UUID) -> Lead:
    lead = session.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="lead not found")
    return lead


@router.get("/dashboard/leads/{lead_id}", response_class=HTMLResponse)
def lead_detail(
    lead_id: uuid.UUID,
    request: Request,
    session: SessionDep,
    user: SessionUser,
    reveal: bool = False,
    moved: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    lead = _get_lead(session, lead_id)
    if reveal:
        add_activity(
            session,
            lead,
            "contact_revealed",
            HUMAN_PREFIX + user.actor,
            {"fields": ["phone", "email"]},
        )
        session.commit()
        session.refresh(lead)
    outbox = list(
        session.scalars(
            select(OutboxMessage).where(OutboxMessage.lead_id == lead.id).order_by(OutboxMessage.id)
        )
    )
    tasks = list(session.scalars(select(Task).where(Task.lead_id == lead.id).order_by(Task.id)))
    crm_job = session.scalars(
        select(Job).where(Job.lead_id == lead.id, Job.kind == "crm_sync").order_by(Job.id.desc())
    ).first()
    show = (lambda v: v or "") if reveal else _mask_contact
    timeline = [
        {
            "at": a.created_at,
            "kind": a.kind,
            "actor": a.actor,
            "summary": m.activity_summary(a.kind, a.detail),
        }
        for a in sorted(lead.activities, key=lambda a: (a.created_at, a.id), reverse=True)
    ]
    return _render(
        request,
        "lead_detail.html",
        {
            "page": "leads",
            "lead": lead,
            "phone": show(lead.phone),
            "email": show(lead.email),
            "message": (lead.message or "") if reveal else mask_pii(lead.message or ""),
            "revealed": reveal,
            "response_seconds": _response_seconds(lead),
            "timeline": timeline,
            "outbox": [
                {
                    "sent_at": o.sent_at,
                    "step": o.step,
                    "channel": o.channel,
                    "to": show(o.to_address),
                    "subject": o.subject,
                    "body": o.body if reveal else mask_pii(o.body),
                    "sender": o.sender,
                }
                for o in outbox
            ],
            "tasks": tasks,
            "crm_job": crm_job,
            "targets": sorted(s.value for s in ALLOWED_TRANSITIONS[LeadStage(lead.stage)]),
            "moved": moved if moved in {s.value for s in LeadStage} else None,
            "error": STAGE_ERRORS.get(error or ""),
        },
        user=user,
    )


@router.post("/dashboard/leads/{lead_id}/stage")
async def lead_stage_submit(
    lead_id: uuid.UUID, request: Request, session: SessionDep, user: SessionUser
) -> Response:
    form = await request.form()
    if not _csrf_ok(request, form.get(CSRF_FIELD)):
        raise HTTPException(status_code=403, detail="invalid form token")
    lead = _get_lead(session, lead_id)
    settings: Settings = request.app.state.settings
    base = f"/dashboard/leads/{lead.id}"
    try:
        target = LeadStage(str(form.get("stage") or ""))
    except ValueError:
        return RedirectResponse(f"{base}?error=invalid_transition", status_code=303)
    try:
        move_stage(
            session,
            lead,
            target,
            actor=HUMAN_PREFIX + user.actor,
            reason=str(form.get("reason") or "")[:200],
            now=utcnow(),
            max_attempts=settings.job_max_attempts,
            crm_enabled=settings.crm_provider != "none",
        )
    except TransitionError as exc:
        session.rollback()
        return RedirectResponse(f"{base}?error={exc.code}", status_code=303)
    session.commit()
    return RedirectResponse(f"{base}?moved={target.value}", status_code=303)
