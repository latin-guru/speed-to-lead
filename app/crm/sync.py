"""Queue a CRM sync for a lead, and run one."""

from __future__ import annotations

import uuid
from datetime import datetime

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.crm.base import CRMAdapter, NotConfigured
from app.crm.hubspot import HubSpotAdapter
from app.crm.mapping import contact_properties, deal_properties
from app.crm.mock import MockHubSpotAdapter
from app.jobs import kinds
from app.jobs.queue import enqueue
from app.models import Activity, Lead
from app.services.timeline import add_activity


def build_crm_adapter(settings: Settings, client: httpx.Client | None = None) -> CRMAdapter | None:
    if settings.crm_provider == "none":
        return None
    if settings.crm_provider == "mock":
        return MockHubSpotAdapter()
    token = settings.hubspot_token.get_secret_value() if settings.hubspot_token else ""
    if not token:
        raise NotConfigured("CRM_PROVIDER=hubspot requires HUBSPOT_TOKEN")
    return HubSpotAdapter(token, client=client or httpx.Client(timeout=10.0))


def enqueue_crm_sync(
    session: Session, lead: Lead, *, max_attempts: int, now: datetime, enabled: bool = True
) -> bool:
    if not enabled:
        return False
    reopens = session.scalar(
        select(func.count(Activity.id)).where(
            Activity.lead_id == lead.id, Activity.kind == "lead_reopened"
        )
    )
    return enqueue(
        session,
        kinds.CRM_SYNC,
        {"lead_id": str(lead.id), "stage": lead.stage},
        idempotency_key=kinds.crm_key(lead.id, lead.stage, int(reopens or 0)),
        max_attempts=max_attempts,
        lead_id=lead.id,
        now=now,
    )


def run_crm_sync(
    session: Session, lead_id: uuid.UUID, adapter: CRMAdapter, *, now: datetime
) -> str:
    """Push the lead's *current* state. Safe to run any number of times.

    If the job fails after a CRM call, the ids are rolled back with the job. The retry then
    upserts again, and neither record is duplicated: contacts match by email (HubSpot's
    unique contact key), and deals are upserted by the unique ``external_lead_id`` property
    (our lead id), so a retried job updates the deal it already created. The MOCK enforces
    the same uniqueness.
    """
    lead = session.get(Lead, lead_id)
    if lead is None:
        return "skipped:lead_missing"
    contact_id = adapter.upsert_contact(contact_properties(lead), external_id=lead.crm_contact_id)
    lead.crm_contact_id = contact_id
    deal_id = adapter.upsert_deal(
        deal_properties(lead), contact_id=contact_id, external_id=lead.crm_deal_id
    )
    lead.crm_deal_id = deal_id
    lead.crm_synced_at = now
    add_activity(
        session,
        lead,
        "crm_synced",
        f"system:{adapter.name}",
        {
            "adapter": adapter.name,
            "stage": lead.stage,
            "contact_id": contact_id,
            "deal_id": deal_id,
        },
    )
    return "synced"
