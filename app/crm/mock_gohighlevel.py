"""MOCK GoHighLevel adapter: an in-memory stand-in. No network, no credentials.

It behaves like GoHighLevel where it matters for the sync:

- contacts are matched by email or phone (the location's duplicate rule), so an upsert of
  the same person updates one contact;
- one opportunity per contact per pipeline: the upsert updates it, and a plain create of a
  second one is rejected, as the real API does;
- every call is recorded in ``calls`` with the method and path the real API would use.
"""

from __future__ import annotations

import itertools

from app.crm.base import CRMError
from app.crm.gohighlevel import EXTERNAL_ID_FIELD, contact_fields, opportunity_fields
from app.crm.mock import MockRecord


class MockGoHighLevelAdapter:
    """MOCK: keeps contacts and opportunities in memory. Nothing is sent anywhere."""

    name = "MOCK-gohighlevel"

    map_contact = staticmethod(contact_fields)
    map_deal = staticmethod(opportunity_fields)

    def __init__(self, pipeline_id: str = "mock-pipeline") -> None:
        self.pipeline_id = pipeline_id
        self.contacts: dict[str, MockRecord] = {}
        self.opportunities: dict[str, MockRecord] = {}
        self.calls: list[tuple[str, str]] = []
        self._ids = itertools.count(5001)

    def _new_id(self, prefix: str) -> str:
        return f"mock-ghl-{prefix}-{next(self._ids)}"

    def _match_contact(self, properties: dict[str, str]) -> MockRecord | None:
        email = (properties.get("email") or "").lower()
        phone = properties.get("phone") or ""
        for record in self.contacts.values():
            if email and (record.properties.get("email") or "").lower() == email:
                return record
            if phone and record.properties.get("phone") == phone:
                return record
        return None

    def upsert_contact(self, properties: dict[str, str], *, external_id: str | None) -> str:
        if external_id is not None:
            self.calls.append(("PUT", f"/contacts/{external_id}"))
            record = self.contacts.get(external_id)
            if record is None:  # mock memory was reset (worker restart); recreate that id
                record = self.contacts[external_id] = MockRecord(external_id, {})
            record.properties.update(properties)
            record.updates += 1
            return record.id
        if not properties.get("email") and not properties.get("phone"):
            raise CRMError("gohighlevel needs an email or phone to upsert a contact")
        self.calls.append(("POST", "/contacts/upsert"))
        record = self._match_contact(properties)
        if record is None:
            record = MockRecord(self._new_id("contact"), dict(properties))
            self.contacts[record.id] = record
        else:
            record.properties.update(properties)
            record.updates += 1
        return record.id

    def _find_opportunity(self, contact_id: str) -> MockRecord | None:
        return next(
            (o for o in self.opportunities.values() if o.properties.get("contactId") == contact_id),
            None,
        )

    def create_opportunity(self, properties: dict[str, str], *, contact_id: str) -> str:
        """A plain create (``POST /opportunities/``). Not used by the sync; it shows that a
        second opportunity for the same contact and pipeline is refused."""
        self.calls.append(("POST", "/opportunities/"))
        if self._find_opportunity(contact_id) is not None:
            raise CRMError("gohighlevel 400: an opportunity for this contact exists")
        record = MockRecord(
            self._new_id("opp"),
            {**properties, "contactId": contact_id, "pipelineId": self.pipeline_id},
        )
        self.opportunities[record.id] = record
        return record.id

    def upsert_deal(
        self, properties: dict[str, str], *, contact_id: str, external_id: str | None
    ) -> str:
        if not properties.get(EXTERNAL_ID_FIELD):
            raise CRMError(f"{EXTERNAL_ID_FIELD} is required for an upsert")
        self.calls.append(("POST", "/opportunities/upsert"))
        record = self._find_opportunity(contact_id)
        if record is None:
            record = MockRecord(
                external_id or self._new_id("opp"),
                {"contactId": contact_id, "pipelineId": self.pipeline_id},
            )
            self.opportunities[record.id] = record
        else:
            record.updates += 1
        record.properties.update(properties)
        record.associations.add(contact_id)
        return record.id
