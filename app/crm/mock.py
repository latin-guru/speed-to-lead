"""MOCK HubSpot adapter: an in-memory stand-in. No network, no credentials.

It behaves like HubSpot where it matters for testing the sync: contacts are unique by email,
updating by id never creates a second record, and every call is recorded in ``calls`` with
the HTTP method and path the real API would have used.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field


@dataclass
class MockRecord:
    id: str
    properties: dict[str, str]
    associations: set[str] = field(default_factory=set)
    updates: int = 0


class MockHubSpotAdapter:
    """MOCK: keeps contacts and deals in memory. Nothing is sent anywhere."""

    name = "MOCK-hubspot"

    def __init__(self) -> None:
        self.contacts: dict[str, MockRecord] = {}
        self.deals: dict[str, MockRecord] = {}
        self.calls: list[tuple[str, str]] = []
        self._ids = itertools.count(1001)

    def _new_id(self, prefix: str) -> str:
        return f"mock-{prefix}-{next(self._ids)}"

    def _upsert(
        self,
        store: dict[str, MockRecord],
        kind: str,
        properties: dict[str, str],
        external_id: str | None,
    ) -> MockRecord:
        if external_id is not None:
            self.calls.append(("PATCH", f"/crm/v3/objects/{kind}s/{external_id}"))
            record = store.get(external_id)
            if record is None:  # mock memory was reset (worker restart); recreate under that id
                record = store[external_id] = MockRecord(external_id, {})
            record.properties.update(properties)
            record.updates += 1
            return record
        self.calls.append(("POST", f"/crm/v3/objects/{kind}s"))
        record = MockRecord(self._new_id(kind), dict(properties))
        store[record.id] = record
        return record

    def upsert_contact(self, properties: dict[str, str], *, external_id: str | None) -> str:
        if external_id is None and properties.get("email"):
            # HubSpot rejects a second contact with the same email; match it instead.
            for record in self.contacts.values():
                if record.properties.get("email") == properties["email"]:
                    external_id = record.id
                    break
        return self._upsert(self.contacts, "contact", properties, external_id).id

    def upsert_deal(
        self, properties: dict[str, str], *, contact_id: str, external_id: str | None
    ) -> str:
        deal = self._upsert(self.deals, "deal", properties, external_id)
        if contact_id not in deal.associations:
            self.calls.append(
                ("PUT", f"/crm/v4/objects/deal/{deal.id}/associations/default/contact/{contact_id}")
            )
            deal.associations.add(contact_id)
        return deal.id
