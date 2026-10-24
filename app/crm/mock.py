"""MOCK HubSpot adapter: an in-memory stand-in. No network, no credentials.

It behaves like HubSpot where it matters for testing the sync:

- contacts are unique by email; deals are unique by the ``external_lead_id`` property
  (HubSpot's ``hasUniqueValue`` custom property), and inserting a second record with the
  same unique value is rejected, exactly like the real API rejects it;
- upserts match on those keys, so a retried sync updates the same records;
- every call is recorded in ``calls`` with the HTTP method and path the real API would use.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from app.crm.base import CRMError
from app.crm.hubspot import EXTERNAL_ID_PROPERTY

UPSERT_DEALS = "/crm/v3/objects/deals/batch/upsert"


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

    # Unique-value enforcement --------------------------------------------------------

    def _find_deal(self, external_lead_id: str) -> MockRecord | None:
        return next(
            (
                d
                for d in self.deals.values()
                if d.properties.get(EXTERNAL_ID_PROPERTY) == external_lead_id
            ),
            None,
        )

    def _insert_deal(self, properties: dict[str, str], deal_id: str | None = None) -> MockRecord:
        unique = properties.get(EXTERNAL_ID_PROPERTY)
        if unique and self._find_deal(unique) is not None:
            raise CRMError(f"hubspot 409: a deal with {EXTERNAL_ID_PROPERTY}={unique} exists")
        record = MockRecord(deal_id or self._new_id("deal"), dict(properties))
        self.deals[record.id] = record
        return record

    def create_deal(self, properties: dict[str, str]) -> str:
        """A plain create (``POST /crm/v3/objects/deals``). Not used by the sync; it shows
        that the unique property makes a duplicate create fail instead of succeed."""
        self.calls.append(("POST", "/crm/v3/objects/deals"))
        return self._insert_deal(properties).id

    # Adapter protocol ----------------------------------------------------------------

    def upsert_contact(self, properties: dict[str, str], *, external_id: str | None) -> str:
        if external_id is None and properties.get("email"):
            # HubSpot rejects a second contact with the same email; match it instead.
            for record in self.contacts.values():
                if record.properties.get("email") == properties["email"]:
                    external_id = record.id
                    break
        if external_id is not None:
            self.calls.append(("PATCH", f"/crm/v3/objects/contacts/{external_id}"))
            existing = self.contacts.get(external_id)
            if existing is None:  # mock memory was reset (worker restart); recreate that id
                existing = self.contacts[external_id] = MockRecord(external_id, {})
            existing.properties.update(properties)
            existing.updates += 1
            return existing.id
        self.calls.append(("POST", "/crm/v3/objects/contacts/batch/upsert"))
        created = MockRecord(self._new_id("contact"), dict(properties))
        self.contacts[created.id] = created
        return created.id

    def upsert_deal(
        self, properties: dict[str, str], *, contact_id: str, external_id: str | None
    ) -> str:
        unique = properties.get(EXTERNAL_ID_PROPERTY)
        if not unique:
            raise CRMError(f"{EXTERNAL_ID_PROPERTY} is required for an upsert")
        self.calls.append(("POST", UPSERT_DEALS))
        deal = self._find_deal(unique)
        if deal is None:
            # external_id: a deal id we saved earlier, if the mock's memory was reset since.
            deal = self._insert_deal(properties, external_id)
        else:
            deal.properties.update(properties)
            deal.updates += 1
        self.calls.append(
            ("PUT", f"/crm/v4/objects/deal/{deal.id}/associations/default/contact/{contact_id}")
        )
        deal.associations.add(contact_id)
        return deal.id
