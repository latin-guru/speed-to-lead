from __future__ import annotations

import hmac
import json
import logging
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import webhook_rate_limit
from app.config import Settings
from app.models import Lead, WebhookEvent
from app.schemas import LeadIn
from app.security.signatures import verify_signature
from app.services.leads import create_lead
from app.services.meta_leadgen import LeadgenChange, PayloadError, lead_fields, parse_payload

log = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])

MAX_BODY_BYTES = 256 * 1024
PROVIDER = "meta"
_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")


@router.get("/meta", response_class=PlainTextResponse)
def meta_verify(
    request: Request,
    mode: str = Query(default="", alias="hub.mode"),
    token: str = Query(default="", alias="hub.verify_token"),
    challenge: str = Query(default="", alias="hub.challenge"),
) -> PlainTextResponse:
    settings: Settings = request.app.state.settings
    expected = settings.meta_verify_token.get_secret_value() if settings.meta_verify_token else ""
    if (
        mode == "subscribe"
        and expected
        and hmac.compare_digest(token.encode(), expected.encode())
        and _CHALLENGE_RE.match(challenge)
    ):
        return PlainTextResponse(challenge)
    raise HTTPException(status_code=403, detail="verification failed")


def _process_change(request: Request, session: Session, change: LeadgenChange) -> str:
    """Handle one leadgen change. Returns created | duplicate | rejected | needs_fetch."""
    seen = session.scalar(
        select(WebhookEvent.id).where(
            WebhookEvent.provider == PROVIDER, WebhookEvent.event_id == change.leadgen_id
        )
    )
    if seen is not None:
        return "duplicate"

    event = WebhookEvent(provider=PROVIDER, event_id=change.leadgen_id, status="processed")
    lead: Lead | None = None
    if change.fields is None:
        event.status, event.reason = "needs_fetch", "no field_data; fetch by leadgen_id"
    else:
        try:
            data = LeadIn.model_validate(lead_fields(change.fields))
        except ValidationError as exc:
            event.status = "rejected"
            event.reason = "; ".join(str(e["msg"]) for e in exc.errors())[:200]
        else:
            lead = create_lead(
                session,
                data,
                actor="webhook:meta",
                detail={
                    "channel": "webhook",
                    "leadgen_id": change.leadgen_id,
                    "form_id": change.form_id,
                    "ad_id": change.ad_id,
                },
            )
            event.lead_id = lead.id
    session.add(event)
    try:
        session.commit()
    except IntegrityError:  # a concurrent delivery won the race
        session.rollback()
        return "duplicate"
    if lead is not None:
        request.app.state.scoring_dispatcher.dispatch(session, lead)
        return "created"
    log.info("webhook_change_skipped", extra={"status": event.status, "reason": event.reason})
    return event.status


def _process_all(request: Request, changes: list[LeadgenChange]) -> dict[str, int]:
    counts = {"created": 0, "duplicate": 0, "rejected": 0, "needs_fetch": 0}
    with request.app.state.session_factory() as session:
        for change in changes:
            counts[_process_change(request, session, change)] += 1
    return counts


@router.post("/meta", dependencies=[Depends(webhook_rate_limit)])
async def meta_receive(request: Request) -> JSONResponse:
    settings: Settings = request.app.state.settings
    if settings.meta_app_secret is None or not settings.meta_app_secret.get_secret_value():
        raise HTTPException(status_code=503, detail="webhook not configured")

    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")
    body = await request.body()  # raw bytes: the signature covers exactly these
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")

    secret = settings.meta_app_secret.get_secret_value().encode()
    if not verify_signature(secret, body, request.headers.get("x-hub-signature-256")):
        log.warning("webhook_signature_invalid", extra={"provider": PROVIDER})
        raise HTTPException(status_code=401, detail="invalid signature")

    try:
        payload: Any = json.loads(body)
        changes = parse_payload(payload)
    except (ValueError, PayloadError) as exc:
        raise HTTPException(status_code=400, detail="malformed payload") from exc

    counts = await run_in_threadpool(_process_all, request, changes)
    return JSONResponse({"received": len(changes), **counts})
