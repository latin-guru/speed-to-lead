"""HubSpot CRM adapter: **skeleton, not used by default**.

It shows the real API shape (CRM v3 objects, v4 default associations, bearer token from a
private app) so swapping the MOCK for the real thing is a config change. It refuses to do
anything without a token (``NotConfigured``). The app only builds it when
``CRM_PROVIDER=hubspot`` and ``HUBSPOT_TOKEN`` are both set.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.crm.base import CRMError, NotConfigured

BASE_URL = "https://api.hubapi.com"


class HubSpotAdapter:
    name = "hubspot"

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
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    # Request shapes (pure; easy to inspect and test) ---------------------------------

    @staticmethod
    def contact_request(
        properties: dict[str, str], external_id: str | None
    ) -> tuple[str, str, dict[str, Any]]:
        if external_id:
            return "PATCH", f"/crm/v3/objects/contacts/{external_id}", {"properties": properties}
        return "POST", "/crm/v3/objects/contacts", {"properties": properties}

    @staticmethod
    def deal_request(
        properties: dict[str, str], external_id: str | None
    ) -> tuple[str, str, dict[str, Any]]:
        if external_id:
            return "PATCH", f"/crm/v3/objects/deals/{external_id}", {"properties": properties}
        return "POST", "/crm/v3/objects/deals", {"properties": properties}

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

    def upsert_contact(self, properties: dict[str, str], *, external_id: str | None) -> str:
        method, path, body = self.contact_request(properties, external_id)
        return str(self._request(method, path, body).get("id") or external_id)

    def upsert_deal(
        self, properties: dict[str, str], *, contact_id: str, external_id: str | None
    ) -> str:
        method, path, body = self.deal_request(properties, external_id)
        deal_id = str(self._request(method, path, body).get("id") or external_id)
        if external_id is None:
            self._request("PUT", self.association_path(deal_id, contact_id), None)
        return deal_id
