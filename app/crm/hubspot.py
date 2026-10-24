"""HubSpot CRM adapter: **skeleton, not used by default**.

It shows the real API shape (CRM v3 objects, v4 default associations, bearer token from a
private app) so swapping the MOCK for the real thing is a config change. It refuses to do
anything without a token (``NotConfigured``). The app only builds it when
``CRM_PROVIDER=hubspot`` and ``HUBSPOT_TOKEN`` are both set.

Idempotency (why a retried sync job cannot create a second deal):

- **Deals are upserted by a unique custom property**, ``external_lead_id`` (our lead's UUID),
  with ``POST /crm/v3/objects/deals/batch/upsert`` and ``idProperty=external_lead_id``.
  HubSpot creates the deal the first time and updates the same deal on every later call,
  even if our process crashed after HubSpot answered and we never saved its deal id.
  The property must exist on the portal with ``hasUniqueValue: true``;
  ``property_setup_request()`` is the one-time request that creates it.
- **Contacts** are upserted by ``email`` (HubSpot's built-in unique contact key), or by
  ``external_lead_id`` for a phone-only lead.
- The v4 *default* association ``PUT`` is idempotent, so it is sent on every sync.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.crm.base import CRMError, NotConfigured
from app.crm.mapping import contact_properties, deal_properties
from app.security.outbound import check_url

BASE_URL = "https://api.hubapi.com"
EXTERNAL_ID_PROPERTY = "external_lead_id"


def property_setup_request(object_type: str) -> tuple[str, str, dict[str, Any]]:
    """One-time setup: the unique ``external_lead_id`` property on ``deals`` / ``contacts``."""
    return (
        "POST",
        f"/crm/v3/properties/{object_type}",
        {
            "name": EXTERNAL_ID_PROPERTY,
            "label": "External lead id",
            "type": "string",
            "fieldType": "text",
            "groupName": "dealinformation" if object_type == "deals" else "contactinformation",
            "hasUniqueValue": True,
        },
    )


class HubSpotAdapter:
    name = "hubspot"

    map_contact = staticmethod(contact_properties)
    map_deal = staticmethod(deal_properties)

    def __init__(
        self,
        token: str | None,
        *,
        client: httpx.Client | None = None,
        base_url: str = BASE_URL,
        timeout: float = 10.0,
    ) -> None:
        self._token = token or None
        self._client = client
        self._base_url = check_url(base_url)
        self._timeout = timeout

    # Request shapes (pure; easy to inspect and test) ---------------------------------

    @staticmethod
    def contact_request(
        properties: dict[str, str], external_id: str | None
    ) -> tuple[str, str, dict[str, Any]]:
        if external_id:
            return "PATCH", f"/crm/v3/objects/contacts/{external_id}", {"properties": properties}
        if properties.get("email"):
            id_property, value = "email", properties["email"]
        else:
            id_property, value = EXTERNAL_ID_PROPERTY, _external_id(properties)
        return (
            "POST",
            "/crm/v3/objects/contacts/batch/upsert",
            _upsert_body(id_property, value, properties),
        )

    @staticmethod
    def deal_request(properties: dict[str, str]) -> tuple[str, str, dict[str, Any]]:
        return (
            "POST",
            "/crm/v3/objects/deals/batch/upsert",
            _upsert_body(EXTERNAL_ID_PROPERTY, _external_id(properties), properties),
        )

    @staticmethod
    def association_path(deal_id: str, contact_id: str) -> str:
        return f"/crm/v4/objects/deal/{deal_id}/associations/default/contact/{contact_id}"

    # Transport ------------------------------------------------------------------------

    def _request(self, method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        if not self._token:
            raise NotConfigured("HubSpotAdapter needs HUBSPOT_TOKEN")
        if self._client is None:
            raise NotConfigured("HubSpotAdapter needs an HTTP client")
        try:
            resp = self._client.request(
                method,
                self._base_url + path,
                json=body,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise CRMError(f"hubspot transport error: {type(exc).__name__}") from exc
        if resp.status_code >= 400:
            raise CRMError(f"hubspot {method} {path.split('/')[4]} -> {resp.status_code}")
        data: Any = resp.json() if resp.content else {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _id(data: dict[str, Any], fallback: str | None) -> str:
        results = data.get("results")
        if isinstance(results, list) and results and isinstance(results[0], dict):
            data = results[0]
        found = data.get("id") or fallback
        if not found:
            raise CRMError("hubspot response had no record id")
        return str(found)

    def upsert_contact(self, properties: dict[str, str], *, external_id: str | None) -> str:
        method, path, body = self.contact_request(properties, external_id)
        return self._id(self._request(method, path, body), external_id)

    def upsert_deal(
        self, properties: dict[str, str], *, contact_id: str, external_id: str | None
    ) -> str:
        """Upsert by ``external_lead_id``; ``external_id`` (a saved deal id) is not needed."""
        method, path, body = self.deal_request(properties)
        deal_id = self._id(self._request(method, path, body), external_id)
        self._request("PUT", self.association_path(deal_id, contact_id), None)
        return deal_id


def _external_id(properties: dict[str, str]) -> str:
    value = properties.get(EXTERNAL_ID_PROPERTY)
    if not value:
        raise CRMError(f"{EXTERNAL_ID_PROPERTY} is required for an upsert")
    return value


def _upsert_body(id_property: str, value: str, properties: dict[str, str]) -> dict[str, Any]:
    return {"inputs": [{"idProperty": id_property, "id": value, "properties": properties}]}
