"""A small JSON-over-HTTPS helper shared by the real (skeleton) CRM adapters."""

from __future__ import annotations

from typing import Any

import httpx

from app.crm.base import CRMError, NotConfigured


def request_json(
    client: httpx.Client | None,
    method: str,
    url: str,
    *,
    label: str,
    headers: dict[str, str],
    body: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> tuple[int, dict[str, Any]]:
    """Send one request; return ``(status, json_object)``. Errors become ``CRMError``.

    Error messages carry the status and the method only, never the payload (which holds
    contact details).
    """
    if client is None:
        raise NotConfigured(f"{label} adapter needs an HTTP client")
    try:
        resp = client.request(method, url, json=body, headers=headers, timeout=timeout)
    except httpx.HTTPError as exc:
        raise CRMError(f"{label} transport error: {type(exc).__name__}") from exc
    if resp.status_code >= 400:
        raise CRMError(f"{label} {method} -> {resp.status_code}")
    data: Any = resp.json() if resp.content else {}
    return resp.status_code, data if isinstance(data, dict) else {}
