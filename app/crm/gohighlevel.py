"""GoHighLevel (LeadConnector API v2) adapter: **skeleton, not used by default**.

A lead becomes a *contact* and an *opportunity* in one pipeline:

- ``POST /contacts/upsert`` creates or updates the contact. GoHighLevel matches an existing
  contact in the location by email or phone (the location's duplicate-contact setting), so
  a retried sync finds the same contact. Our lead id is written to the
  ``external_lead_id`` contact custom field. Once we know the contact id, later syncs use
  ``PUT /contacts/{id}``.
- ``POST /opportunities/upsert`` creates or updates the opportunity for that contact in
  ``GHL_PIPELINE_ID`` (GoHighLevel keeps one opportunity per contact per pipeline), with
  the stage from ``GHL_STAGE_IDS`` and status open / won / lost. A retry updates the same
  opportunity.

Auth is a private integration token (``GHL_TOKEN``) for one location (``GHL_LOCATION_ID``),
sent with the ``Version`` header the v2 API requires. Without them the adapter raises
``NotConfigured``; the app only builds it when ``CRM_PROVIDER=gohighlevel`` and
``CRM_MOCK=false``.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.crm.base import CRMError, NotConfigured
from app.crm.http import request_json
from app.crm.mapping import split_name
from app.models import Lead, LeadStage
from app.security.outbound import check_url

BASE_URL = "https://services.leadconnectorhq.com"
API_VERSION = "2021-07-28"
EXTERNAL_ID_FIELD = "external_lead_id"
STAGES = tuple(s.value for s in LeadStage)
STATUS: dict[str, str] = {
    LeadStage.NEW: "open",
    LeadStage.QUALIFIED: "open",
    LeadStage.CONTACTED: "open",
    LeadStage.WON: "won",
    LeadStage.LOST: "lost",
}


def parse_stage_ids(spec: str) -> dict[str, str]:
    """``"new=abc,qualified=def,..."`` -> {stage: pipeline stage id}. Every stage is needed."""
    found: dict[str, str] = {}
    for item in spec.split(","):
        if not item.strip():
            continue
        stage, sep, stage_id = item.partition("=")
        stage, stage_id = stage.strip().lower(), stage_id.strip()
        if not sep or stage not in STAGES or not stage_id:
            raise ValueError(f"bad GHL_STAGE_IDS entry {item.strip()!r} (use stage=id)")
        found[stage] = stage_id
    missing = [s for s in STAGES if s not in found]
    if missing:
        raise ValueError(f"GHL_STAGE_IDS is missing: {', '.join(missing)}")
    return found


def contact_fields(lead: Lead) -> dict[str, str]:
    first, last = split_name(lead.name)
    fields = {
        "firstName": first,
        "lastName": last,
        "source": lead.source,
        EXTERNAL_ID_FIELD: str(lead.id),
    }
    if lead.email:
        fields["email"] = lead.email
    if lead.phone:
        fields["phone"] = lead.phone
    return fields


def opportunity_fields(lead: Lead) -> dict[str, str]:
    fields = {
        "name": f"{lead.name} - {lead.source}",
        "stage": lead.stage,
        "status": STATUS[lead.stage],
        EXTERNAL_ID_FIELD: str(lead.id),
    }
    if lead.score is not None:
        fields["lead_score"] = str(lead.score)
    return fields


def _custom_fields(fields: dict[str, str], keys: tuple[str, ...]) -> list[dict[str, str]]:
    return [{"key": k, "field_value": fields[k]} for k in keys if k in fields]


class GoHighLevelAdapter:
    name = "gohighlevel"

    map_contact = staticmethod(contact_fields)
    map_deal = staticmethod(opportunity_fields)

    def __init__(
        self,
        token: str | None,
        *,
        location_id: str | None,
        pipeline_id: str | None,
        stage_ids: dict[str, str] | None,
        client: httpx.Client | None = None,
        base_url: str = BASE_URL,
        timeout: float = 10.0,
    ) -> None:
        self._token = token or None
        self._location_id = location_id or None
        self._pipeline_id = pipeline_id or None
        self._stage_ids = stage_ids or {}
        self._client = client
        self._base_url = check_url(base_url)
        self._timeout = timeout

    # Request shapes (pure; easy to inspect and test) ---------------------------------

    def contact_request(
        self, fields: dict[str, str], external_id: str | None
    ) -> tuple[str, str, dict[str, Any]]:
        body: dict[str, Any] = {
            k: v for k, v in fields.items() if k in ("firstName", "lastName", "email", "phone")
        }
        body["source"] = fields.get("source", "")
        body["customFields"] = _custom_fields(fields, (EXTERNAL_ID_FIELD,))
        if external_id:
            return "PUT", f"/contacts/{external_id}", body
        if not fields.get("email") and not fields.get("phone"):
            raise CRMError("gohighlevel needs an email or phone to upsert a contact")
        return "POST", "/contacts/upsert", {"locationId": self._location_id, **body}

    def opportunity_request(
        self, fields: dict[str, str], contact_id: str
    ) -> tuple[str, str, dict[str, Any]]:
        stage = fields.get("stage", "")
        if stage not in self._stage_ids:
            raise NotConfigured(f"GHL_STAGE_IDS has no pipeline stage for {stage!r}")
        return (
            "POST",
            "/opportunities/upsert",
            {
                "locationId": self._location_id,
                "pipelineId": self._pipeline_id,
                "contactId": contact_id,
                "name": fields.get("name", ""),
                "status": fields.get("status", "open"),
                "pipelineStageId": self._stage_ids[stage],
                "customFields": _custom_fields(fields, (EXTERNAL_ID_FIELD, "lead_score")),
            },
        )

    # Transport ------------------------------------------------------------------------

    def _send(self, method: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
        if not self._token or not self._location_id or not self._pipeline_id:
            raise NotConfigured(
                "GoHighLevelAdapter needs GHL_TOKEN, GHL_LOCATION_ID and GHL_PIPELINE_ID"
            )
        _, data = request_json(
            self._client,
            method,
            self._base_url + path,
            label="gohighlevel",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Version": API_VERSION,
                "Accept": "application/json",
            },
            body=body,
            timeout=self._timeout,
        )
        return data

    @staticmethod
    def _id(data: dict[str, Any], key: str, fallback: str | None) -> str:
        record = data.get(key)
        found = record.get("id") if isinstance(record, dict) else None
        if not found and not fallback:
            raise CRMError(f"gohighlevel response had no {key} id")
        return str(found or fallback)

    def upsert_contact(self, properties: dict[str, str], *, external_id: str | None) -> str:
        method, path, body = self.contact_request(properties, external_id)
        return self._id(self._send(method, path, body), "contact", external_id)

    def upsert_deal(
        self, properties: dict[str, str], *, contact_id: str, external_id: str | None
    ) -> str:
        method, path, body = self.opportunity_request(properties, contact_id)
        return self._id(self._send(method, path, body), "opportunity", external_id)
