from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.deps import SessionDep, public_rate_limit, too_many
from app.schemas import LeadIn, LeadOut
from app.services.leads import (
    create_lead,
    leads_today_for_phone,
    seconds_until_utc_midnight,
    to_lead_out,
)

router = APIRouter(prefix="/api", tags=["leads"])


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
    session.commit()
    request.app.state.scoring_dispatcher.dispatch(session, lead)
    return to_lead_out(lead)
