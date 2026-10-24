"""Inbound SMS replies from a (mock) messaging provider.

``POST /webhooks/inbound/sms`` with ``X-Signature-256: sha256=<hmac of the raw body>`` under
``INBOUND_WEBHOOK_SECRET``. Body: ``{"message_id": ..., "from": "+1...", "body": ...}``.

- A STOP-style keyword opts the number out and cancels every pending send for it.
- Any other reply means a person is talking to us: pause automation and hand off.
- Each ``message_id`` is processed once.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import webhook_rate_limit
from app.config import Settings
from app.followups.engine import is_stop_keyword, opt_out
from app.models import Lead, WebhookEvent, utcnow
from app.schemas import InboundReplyIn
from app.security.signatures import verify_signature
from app.services.handoff import LEAD_REPLIED, handoff
from app.services.normalize import InvalidInputError, normalize_us_phone
from app.services.timeline import add_activity

log = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks/inbound", tags=["webhooks"])

PROVIDER = "inbound_sms"
MAX_BODY_BYTES = 16 * 1024
SIGNATURE_HEADER = "x-signature-256"


def _process(session: Session, reply: InboundReplyIn, phone: str) -> str:
    seen = session.scalar(
        select(WebhookEvent.id).where(
            WebhookEvent.provider == PROVIDER, WebhookEvent.event_id == reply.message_id
        )
    )
    if seen is not None:
        return "duplicate"
    now = utcnow()
    event = WebhookEvent(provider=PROVIDER, event_id=reply.message_id, status="processed")
    if is_stop_keyword(reply.body):
        leads = opt_out(session, phone, now=now, source="inbound:sms")
        outcome = "opted_out" if leads else "unmatched"
        event.lead_id = leads[0].id if leads else None
    else:
        lead = session.scalars(
            select(Lead).where(Lead.phone == phone).order_by(Lead.created_at.desc()).limit(1)
        ).first()
        if lead is None:
            outcome = "unmatched"
        else:
            add_activity(
                session, lead, "inbound_reply", "lead", {"channel": "sms", "body": reply.body[:500]}
            )
            handoff(session, lead, LEAD_REPLIED, now=now, detail=reply.body[:300])
            event.lead_id = lead.id
            outcome = "handoff"
    if outcome == "unmatched":
        event.status, event.reason = "unmatched", "no lead with this phone"
    session.add(event)
    try:
        session.commit()
    except IntegrityError:  # a concurrent delivery of the same message won
        session.rollback()
        return "duplicate"
    log.info("inbound_reply", extra={"outcome": outcome})
    return outcome


@router.post("/sms", dependencies=[Depends(webhook_rate_limit)])
async def inbound_sms(request: Request) -> JSONResponse:
    settings: Settings = request.app.state.settings
    secret = settings.inbound_webhook_secret
    if secret is None or not secret.get_secret_value():
        raise HTTPException(status_code=503, detail="inbound webhook not configured")
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")
    if not verify_signature(
        secret.get_secret_value().encode(), body, request.headers.get(SIGNATURE_HEADER)
    ):
        log.warning("webhook_signature_invalid", extra={"provider": PROVIDER})
        raise HTTPException(status_code=401, detail="invalid signature")
    try:
        reply = InboundReplyIn.model_validate(json.loads(body))
        phone = normalize_us_phone(reply.from_phone)
    except (ValueError, ValidationError, InvalidInputError) as exc:
        raise HTTPException(status_code=400, detail="malformed payload") from exc

    def run() -> str:
        with request.app.state.session_factory() as session:
            return _process(session, reply, phone)

    outcome = await run_in_threadpool(run)
    return JSONResponse({"outcome": outcome})
