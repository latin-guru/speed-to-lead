"""HMAC-SHA256 webhook signature verification (Meta ``X-Hub-Signature-256`` style)."""

from __future__ import annotations

import hashlib
import hmac

SIGNATURE_PREFIX = "sha256="


def sign_body(secret: bytes, body: bytes) -> str:
    return SIGNATURE_PREFIX + hmac.new(secret, body, hashlib.sha256).hexdigest()


def verify_signature(secret: bytes, body: bytes, header_value: str | None) -> bool:
    """True only if ``header_value`` is ``sha256=<hex>`` of ``body`` under ``secret``.

    ``body`` must be the raw request bytes exactly as received (never re-serialized JSON).
    Comparison is constant-time.
    """
    if not secret or not header_value or not header_value.startswith(SIGNATURE_PREFIX):
        return False
    expected = sign_body(secret, body)
    return hmac.compare_digest(expected.encode(), header_value.strip().encode())
