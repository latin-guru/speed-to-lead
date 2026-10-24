"""The CRM adapter protocol."""

from __future__ import annotations

from typing import Protocol


class NotConfigured(RuntimeError):  # noqa: N818  (reads better at call sites)
    """The adapter is missing credentials or configuration."""


class CRMError(RuntimeError):
    """The CRM rejected or failed a request (the sync job retries with backoff)."""


class CRMAdapter(Protocol):
    """Create-or-update a contact and its deal. Both calls must be idempotent:

    calling them again with the same ``external_id`` updates the same record instead of
    creating a second one.
    """

    name: str

    def upsert_contact(self, properties: dict[str, str], *, external_id: str | None) -> str:
        """Return the CRM's contact id."""
        ...

    def upsert_deal(
        self, properties: dict[str, str], *, contact_id: str, external_id: str | None
    ) -> str:
        """Return the CRM's deal id (associated with ``contact_id``)."""
        ...
