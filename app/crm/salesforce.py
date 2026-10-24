"""Salesforce adapter (REST API): **skeleton, not used by default**.

A lead becomes a Salesforce **Lead** (the person) and an **Opportunity** (the deal), both
upserted by an External ID field holding our lead's UUID:

- ``PATCH /services/data/<version>/sobjects/Lead/External_Lead_Id__c/<uuid>``
- ``PATCH /services/data/<version>/sobjects/Opportunity/External_Lead_Id__c/<uuid>``

An upsert by external id creates the record the first time (201) and updates the same
record every later time (200, or 204 on older API versions), so a retried sync job can
never create a duplicate, even if it crashed after Salesforce answered. One-time setup in
the org: a text field ``External_Lead_Id__c`` marked *External ID* and *Unique* on both
objects, and a ``Lead__c`` lookup (to Lead) on Opportunity.

Auth: an OAuth access token (``SALESFORCE_ACCESS_TOKEN``) and the org's My Domain URL
(``SALESFORCE_INSTANCE_URL``, checked against the outbound allowlist). Getting and
refreshing the token (JWT bearer flow or similar) is out of scope for this skeleton.
Without them it raises ``NotConfigured``; the app only builds it when
``CRM_PROVIDER=salesforce`` and ``CRM_MOCK=false``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx

from app.crm.base import CRMError, NotConfigured
from app.crm.http import request_json
from app.crm.mapping import split_name
from app.models import Lead, LeadStage
from app.security.outbound import check_url

EXTERNAL_ID_FIELD = "External_Lead_Id__c"
DEFAULT_API_VERSION = "v61.0"
# A Salesforce Lead needs a Company; homeowners have none.
INDIVIDUAL = "(individual)"

LEAD_STATUS: dict[str, str] = {
    LeadStage.NEW: "Open - Not Contacted",
    LeadStage.QUALIFIED: "Working - Contacted",
    LeadStage.CONTACTED: "Working - Contacted",
    LeadStage.WON: "Closed - Converted",
    LeadStage.LOST: "Closed - Not Converted",
}
OPPORTUNITY_STAGE: dict[str, str] = {
    LeadStage.NEW: "Prospecting",
    LeadStage.QUALIFIED: "Qualification",
    LeadStage.CONTACTED: "Needs Analysis",
    LeadStage.WON: "Closed Won",
    LeadStage.LOST: "Closed Lost",
}
CLOSE_AFTER = timedelta(days=30)


def lead_fields(lead: Lead) -> dict[str, str]:
    first, last = split_name(lead.name)
    fields = {
        "FirstName": first,
        "LastName": last or first or "Unknown",  # LastName is required
        "Company": INDIVIDUAL,
        "LeadSource": lead.source,
        "Status": LEAD_STATUS[lead.stage],
        EXTERNAL_ID_FIELD: str(lead.id),
    }
    if lead.email:
        fields["Email"] = lead.email
    if lead.phone:
        fields["Phone"] = lead.phone
    return fields


def opportunity_fields(lead: Lead) -> dict[str, str]:
    fields = {
        "Name": f"{lead.name} - {lead.source}",
        "StageName": OPPORTUNITY_STAGE[lead.stage],
        "CloseDate": (lead.created_at + CLOSE_AFTER).date().isoformat(),
        "LeadSource": lead.source,
        EXTERNAL_ID_FIELD: str(lead.id),
    }
    if lead.stage == LeadStage.LOST and lead.lost_reason:
        fields["Loss_Reason__c"] = lead.lost_reason
    return fields


def _external_id(fields: dict[str, str]) -> str:
    value = fields.get(EXTERNAL_ID_FIELD)
    if not value:
        raise CRMError(f"{EXTERNAL_ID_FIELD} is required for an upsert")
    return value


class SalesforceAdapter:
    name = "salesforce"

    map_contact = staticmethod(lead_fields)
    map_deal = staticmethod(opportunity_fields)

    def __init__(
        self,
        access_token: str | None,
        *,
        instance_url: str | None,
        api_version: str = DEFAULT_API_VERSION,
        client: httpx.Client | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._token = access_token or None
        self._instance = check_url(instance_url) if instance_url else None
        self._version = api_version
        self._client = client
        self._timeout = timeout

    # Request shapes (pure) ------------------------------------------------------------

    def upsert_request(
        self, sobject: str, fields: dict[str, str]
    ) -> tuple[str, str, dict[str, Any]]:
        external = _external_id(fields)
        body = {k: v for k, v in fields.items() if k != EXTERNAL_ID_FIELD}
        path = f"/services/data/{self._version}/sobjects/{sobject}/{EXTERNAL_ID_FIELD}/{external}"
        return "PATCH", path, body

    # Transport ------------------------------------------------------------------------

    def _send(
        self, method: str, path: str, body: dict[str, Any] | None
    ) -> tuple[int, dict[str, Any]]:
        if not self._token or not self._instance:
            raise NotConfigured(
                "SalesforceAdapter needs SALESFORCE_ACCESS_TOKEN and SALESFORCE_INSTANCE_URL"
            )
        return request_json(
            self._client,
            method,
            self._instance + path,
            label="salesforce",
            headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
            body=body,
            timeout=self._timeout,
        )

    def _upsert(self, sobject: str, fields: dict[str, str], known_id: str | None) -> str:
        method, path, body = self.upsert_request(sobject, fields)
        _, data = self._send(method, path, body)
        if data.get("success") is False:
            raise CRMError(f"salesforce {sobject} upsert was not successful")
        found = data.get("id") or known_id
        if not found:  # 204 No Content on older API versions: read the id back
            _, record = self._send("GET", path + "?fields=Id", None)
            found = record.get("Id")
        if not found:
            raise CRMError(f"salesforce {sobject} response had no id")
        return str(found)

    def upsert_contact(self, properties: dict[str, str], *, external_id: str | None) -> str:
        return self._upsert("Lead", properties, external_id)

    def upsert_deal(
        self, properties: dict[str, str], *, contact_id: str, external_id: str | None
    ) -> str:
        return self._upsert("Opportunity", {**properties, "Lead__c": contact_id}, external_id)
