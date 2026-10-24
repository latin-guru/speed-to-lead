"""The lead timeline (``activities``)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models import Activity, Lead


def add_activity(
    session: Session, lead: Lead, kind: str, actor: str, detail: dict[str, Any] | None = None
) -> Activity:
    activity = Activity(lead_id=lead.id, kind=kind, actor=actor[:32], detail=detail or {})
    session.add(activity)
    return activity
