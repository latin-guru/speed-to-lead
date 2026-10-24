from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.deps import AdminDep, SessionDep, public_rate_limit, too_many
from app.config import Settings
from app.models import Lead, LeadStage, utcnow
from app.schemas import ContactLogIn, LeadDetailOut, LeadIn, LeadOut, StageChangeIn
from app.services.leads import (
    create_lead,
    leads_today_for_phone,
    seconds_until_utc_midnight,
    to_lead_detail,
    to_lead_out,
)
from app.services.pipeline import HUMAN_PREFIX, TransitionError, log_human_contact, move_stage

router = APIRouter(prefix="/api", tags=["leads"])

TRANSITION_STATUS = {
    "invalid_transition": 409,
    "same_stage": 409,
    "reason_required": 422,
    "human_only": 403,
}


def enforce_phone_cap(request: Request, session: Session, phone: str | None) -> None:
    if phone is None:
        return
    cap: int = request.app.state.settings.phone_daily_cap
    if leads_today_for_phone(session, phone) >= cap:
        raise too_many(seconds_until_utc_midnight(), "daily limit reached for this phone number")


@router.post(
    "/leads", status_code=201, response_model=LeadOut, dependencies=[Depends(public_rate_limit)]
)
def post_lead(payload: LeadIn, request: Request, session: SessionDep) -> LeadOut:
    enforce_phone_cap(request, session, payload.phone)
    lead = create_lead(session, payload, actor="api", detail={"channel": "api"})
    request.app.state.intake.on_lead_created(session, lead)
    session.commit()
    return to_lead_out(lead)  # scoring is null here; the worker fills it in moments later


def _get_lead(session: Session, lead_id: uuid.UUID) -> Lead:
    lead = session.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="lead not found")
    return lead


@router.get("/leads/{lead_id}", response_model=LeadDetailOut, dependencies=[AdminDep])
def get_lead(lead_id: uuid.UUID, session: SessionDep) -> LeadDetailOut:
    return to_lead_detail(_get_lead(session, lead_id))


@router.patch("/leads/{lead_id}/stage", response_model=LeadDetailOut, dependencies=[AdminDep])
def patch_stage(
    lead_id: uuid.UUID, body: StageChangeIn, request: Request, session: SessionDep
) -> LeadDetailOut:
    settings: Settings = request.app.state.settings
    lead = _get_lead(session, lead_id)
    try:
        move_stage(
            session,
            lead,
            LeadStage(body.stage),
            actor=HUMAN_PREFIX + body.actor,
            reason=body.reason,
            now=utcnow(),
            max_attempts=settings.job_max_attempts,
            crm_enabled=settings.crm_provider != "none",
        )
    except TransitionError as exc:
        session.rollback()
        raise HTTPException(
            status_code=TRANSITION_STATUS[exc.code], detail={"code": exc.code, "message": str(exc)}
        ) from exc
    session.commit()
    session.refresh(lead)
    return to_lead_detail(lead)


@router.post("/leads/{lead_id}/contacts", response_model=LeadDetailOut, dependencies=[AdminDep])
def post_contact(lead_id: uuid.UUID, body: ContactLogIn, session: SessionDep) -> LeadDetailOut:
    """Log that a person reached the lead (sets first response, stops automation)."""
    lead = _get_lead(session, lead_id)
    log_human_contact(
        session,
        lead,
        actor=HUMAN_PREFIX + body.actor,
        channel=body.channel,
        note=body.note,
        now=utcnow(),
    )
    session.commit()
    session.refresh(lead)
    return to_lead_detail(lead)
