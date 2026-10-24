"""The CRM adapter protocol."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # pragma: no cover
    from app.models import Lead


class NotConfigured(RuntimeError):  # noqa: N818  (reads better at call sites)
    """The adapter is missing credentials or configuration."""


class CRMError(RuntimeError):
    """The CRM rejected or failed a request (the sync job retries with backoff)."""


class CRMAdapter(Protocol):
    """Create-or-update a contact and its deal. Both calls must be idempotent:

    calling them again for the same lead updates the same records instead of creating
    second ones (each adapter documents the key it upserts by). ``map_contact`` and
    ``map_deal`` turn a lead into that CRM's field names.
    """

    name: str

    def map_contact(self, lead: Lead) -> dict[str, str]: ...

    def map_deal(self, lead: Lead) -> dict[str, str]: ...

    def upsert_contact(self, properties: dict[str, str], *, external_id: str | None) -> str:
        """Return the CRM's contact id."""
        ...

    def upsert_deal(
        self, properties: dict[str, str], *, contact_id: str, external_id: str | None
    ) -> str:
        """Return the CRM's deal id (associated with ``contact_id``)."""
        ...
