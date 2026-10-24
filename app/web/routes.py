"""Server-rendered lead form at ``/`` (CSRF double-submit token + honeypot field)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app.api.deps import SessionDep, public_rate_limit
from app.api.leads import enforce_phone_cap
from app.config import Settings
from app.schemas import LeadIn
from app.security.csrf import CSRF_COOKIE, CSRF_FIELD, check_double_submit, issue_token
from app.services.leads import create_lead

log = logging.getLogger(__name__)
router = APIRouter(tags=["web"])
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

HONEYPOT_FIELD = "website"
FORM_FIELDS = ("name", "email", "phone", "message")


def _render_form(
    request: Request,
    *,
    status_code: int = 200,
    errors: list[str] | None = None,
    values: dict[str, str] | None = None,
) -> HTMLResponse:
    settings: Settings = request.app.state.settings
    token = issue_token(settings.secret_key_bytes)
    response = templates.TemplateResponse(
        request,
        "form.html",
        {
            "company": settings.company_name,
            "csrf_field": CSRF_FIELD,
            "csrf_token": token,
            "honeypot": HONEYPOT_FIELD,
            "errors": errors or [],
            "values": values or {},
        },
        status_code=status_code,
    )
    response.set_cookie(
        CSRF_COOKIE,
        token,
        httponly=True,
        samesite="strict",
        secure=settings.environment == "prod",
        max_age=2 * 60 * 60,
    )
    return response


@router.get("/", response_class=HTMLResponse)
def form_page(request: Request) -> HTMLResponse:
    return _render_form(request)


@router.get("/thanks", response_class=HTMLResponse)
def thanks_page(request: Request) -> HTMLResponse:
    settings: Settings = request.app.state.settings
    return templates.TemplateResponse(request, "thanks.html", {"company": settings.company_name})


def _error_messages(exc: ValidationError) -> list[str]:
    out: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"] if p != "body")
        msg = str(err["msg"]).removeprefix("Value error, ")
        out.append(f"{loc}: {msg}" if loc else msg)
    return out


@router.post("/", response_class=HTMLResponse, dependencies=[Depends(public_rate_limit)])
async def form_submit(request: Request, session: SessionDep) -> Response:
    settings: Settings = request.app.state.settings
    form = await request.form()
    values: dict[str, str] = {k: str(form.get(k) or "") for k in FORM_FIELDS}

    if not check_double_submit(
        settings.secret_key_bytes, request.cookies.get(CSRF_COOKIE), str(form.get(CSRF_FIELD) or "")
    ):
        return _render_form(
            request,
            status_code=403,
            errors=["Your session expired. Please try again."],
            values=values,
        )

    if str(form.get(HONEYPOT_FIELD) or "").strip():
        log.info("honeypot_triggered", extra={"source": "web"})
        return RedirectResponse("/thanks", status_code=303)  # look successful to the bot

    payload: dict[str, Any] = {k: v for k, v in values.items() if v.strip()}
    payload.setdefault("name", "")
    payload["source"] = "web"
    try:
        data = LeadIn.model_validate(payload)
    except ValidationError as exc:
        return _render_form(request, status_code=422, errors=_error_messages(exc), values=values)

    try:
        enforce_phone_cap(request, session, data.phone)
    except HTTPException as exc:
        response = _render_form(
            request,
            status_code=429,
            errors=["We already have your request for today. We will be in touch soon."],
            values=values,
        )
        response.headers.update(exc.headers or {})
        return response

    lead = create_lead(session, data, actor="web_form", detail={"channel": "web_form"})
    request.app.state.intake.on_lead_created(session, lead)
    session.commit()
    return RedirectResponse("/thanks", status_code=303)
