"""MOCK Salesforce adapter: an in-memory stand-in. No network, no credentials.

Records live per sObject (``Lead``, ``Opportunity``) and are keyed by the unique External
ID field ``External_Lead_Id__c``, like the real org after the one-time setup:

- an upsert by external id creates the record once and updates it afterwards;
- a plain insert with an external id that already exists fails with ``DUPLICATE_VALUE``;
- every call is recorded in ``calls`` with the method and path the real API would use.
"""

from __future__ import annotations

import itertools

from app.crm.base import CRMError
from app.crm.mock import MockRecord
from app.crm.salesforce import (
    DEFAULT_API_VERSION,
    EXTERNAL_ID_FIELD,
    lead_fields,
    opportunity_fields,
)

BASE = f"/services/data/{DEFAULT_API_VERSION}/sobjects"


class MockSalesforceAdapter:
    """MOCK: keeps Leads and Opportunities in memory. Nothing is sent anywhere."""

    name = "MOCK-salesforce"

    map_contact = staticmethod(lead_fields)
    map_deal = staticmethod(opportunity_fields)

    def __init__(self) -> None:
        self.records: dict[str, dict[str, MockRecord]] = {"Lead": {}, "Opportunity": {}}
        self.calls: list[tuple[str, str]] = []
        self._ids = itertools.count(1)

    def _new_id(self, sobject: str) -> str:
        prefix = "00Q" if sobject == "Lead" else "006"  # Salesforce key prefixes
        return f"{prefix}MOCK{next(self._ids):011d}"

    def _by_external(self, sobject: str, external: str) -> MockRecord | None:
        return next(
            (
                r
                for r in self.records[sobject].values()
                if r.properties.get(EXTERNAL_ID_FIELD) == external
            ),
            None,
        )

    def insert(self, sobject: str, fields: dict[str, str]) -> str:
        """A plain create (``POST .../sobjects/<type>``). Not used by the sync; it shows that
        the unique external id makes a duplicate insert fail instead of succeed."""
        self.calls.append(("POST", f"{BASE}/{sobject}"))
        external = fields.get(EXTERNAL_ID_FIELD, "")
        if external and self._by_external(sobject, external) is not None:
            raise CRMError(f"salesforce 400 DUPLICATE_VALUE: {EXTERNAL_ID_FIELD}={external}")
        record = MockRecord(self._new_id(sobject), dict(fields))
        self.records[sobject][record.id] = record
        return record.id

    def _upsert(self, sobject: str, fields: dict[str, str]) -> str:
        external = fields.get(EXTERNAL_ID_FIELD)
        if not external:
            raise CRMError(f"{EXTERNAL_ID_FIELD} is required for an upsert")
        self.calls.append(("PATCH", f"{BASE}/{sobject}/{EXTERNAL_ID_FIELD}/{external}"))
        record = self._by_external(sobject, external)
        if record is None:
            record = MockRecord(self._new_id(sobject), dict(fields))
            self.records[sobject][record.id] = record
        else:
            record.properties.update(fields)
            record.updates += 1
        return record.id

    def upsert_contact(self, properties: dict[str, str], *, external_id: str | None) -> str:
        return self._upsert("Lead", properties)

    def upsert_deal(
        self, properties: dict[str, str], *, contact_id: str, external_id: str | None
    ) -> str:
        deal_id = self._upsert("Opportunity", {**properties, "Lead__c": contact_id})
        self.records["Opportunity"][deal_id].associations.add(contact_id)
        return deal_id
