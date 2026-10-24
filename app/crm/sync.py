"""Queue a CRM sync for a lead, and run one."""

from __future__ import annotations

import uuid
from datetime import datetime

import httpx
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.crm.base import CRMAdapter, NotConfigured
from app.crm.gohighlevel import GoHighLevelAdapter, parse_stage_ids
from app.crm.hubspot import HubSpotAdapter
from app.crm.mock import MockHubSpotAdapter
from app.crm.mock_gohighlevel import MockGoHighLevelAdapter
from app.crm.mock_salesforce import MockSalesforceAdapter
from app.crm.salesforce import SalesforceAdapter
from app.jobs import kinds
from app.jobs.queue import enqueue
from app.models import Activity, Lead
from app.services.timeline import add_activity


def _secret(value: SecretStr | None) -> str:
    return value.get_secret_value() if value is not None else ""


def build_crm_adapter(settings: Settings, client: httpx.Client | None = None) -> CRMAdapter | None:
    """The adapter for ``CRM_PROVIDER``: its MOCK unless ``CRM_MOCK=false``.

    The real adapters are skeletons that need credentials; asking for one without them
    fails at startup (``NotConfigured``) rather than at the first sync.
    """
    provider = "hubspot" if settings.crm_provider == "mock" else settings.crm_provider
    if provider == "none":
        return None
    if settings.crm_mock or settings.crm_provider == "mock":
        mocks: dict[str, type[CRMAdapter]] = {
            "hubspot": MockHubSpotAdapter,
            "gohighlevel": MockGoHighLevelAdapter,
            "salesforce": MockSalesforceAdapter,
        }
        return mocks[provider]()
    http = client or httpx.Client(timeout=10.0)
    if provider == "hubspot":
        if not _secret(settings.hubspot_token):
            raise NotConfigured("CRM_PROVIDER=hubspot requires HUBSPOT_TOKEN")
        return HubSpotAdapter(_secret(settings.hubspot_token), client=http)
    if provider == "gohighlevel":
        if not (
            _secret(settings.ghl_token) and settings.ghl_location_id and settings.ghl_pipeline_id
        ):
            raise NotConfigured(
                "CRM_PROVIDER=gohighlevel requires GHL_TOKEN, GHL_LOCATION_ID and GHL_PIPELINE_ID"
            )
        try:
            stage_ids = parse_stage_ids(settings.ghl_stage_ids)
        except ValueError as exc:
            raise NotConfigured(str(exc)) from exc
        return GoHighLevelAdapter(
            _secret(settings.ghl_token),
            location_id=settings.ghl_location_id,
            pipeline_id=settings.ghl_pipeline_id,
            stage_ids=stage_ids,
            client=http,
        )
    if not (_secret(settings.salesforce_access_token) and settings.salesforce_instance_url):
        raise NotConfigured(
            "CRM_PROVIDER=salesforce requires SALESFORCE_ACCESS_TOKEN and SALESFORCE_INSTANCE_URL"
        )
    return SalesforceAdapter(
        _secret(settings.salesforce_access_token),
        instance_url=settings.salesforce_instance_url,
        api_version=settings.salesforce_api_version,
        client=http,
    )


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
    contact_id = adapter.upsert_contact(adapter.map_contact(lead), external_id=lead.crm_contact_id)
    lead.crm_contact_id = contact_id
    deal_id = adapter.upsert_deal(
        adapter.map_deal(lead), contact_id=contact_id, external_id=lead.crm_deal_id
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
