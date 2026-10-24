"""Field-level encryption for contact details, plus a blind index for lookups.

Phone numbers and emails are stored **encrypted** (Fernet: AES-128-CBC + HMAC-SHA256, with a
random IV per value), so a copy of the database alone does not reveal them. Because the
ciphertext is different every time, it cannot be searched. Each encrypted column therefore
has a **blind index** next to it: an HMAC-SHA256 of the normalized value under a separate
key. Equal values give equal indexes, so the app can still find "every lead with this
phone" (per-phone caps, STOP matching, "one voice" frequency limits) without decrypting
anything or storing the plaintext.

Keys come from the environment:

- ``FIELD_ENCRYPTION_KEY``: one or more Fernet keys, comma-separated, **newest first**. New
  values are encrypted with the first key; every key in the list can decrypt. To rotate:
  put a new key in front, deploy, run ``python -m app.cli rotate-keys`` (re-encrypts every
  row with the new key), then drop the old key.
- ``BLIND_INDEX_KEY``: the HMAC key for the blind index. It is not rotated by that command,
  since changing it means recomputing every index (a new migration).

The rest of the app reads and writes ``lead.phone`` / ``lead.email`` as plain strings; the
model properties call into this module.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Sequence
from typing import TYPE_CHECKING

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

if TYPE_CHECKING:  # pragma: no cover
    from app.config import Settings

# Dev/test fallback when no key is configured: derived from a public string, so it is
# obviously not a secret. ``/readyz`` warns about it, and ENVIRONMENT=prod refuses it.
_DEV_LABEL = b"speed-to-lead: insecure development-only key, never use in production"


def dev_key(purpose: str) -> str:
    digest = hashlib.sha256(_DEV_LABEL + b"|" + purpose.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode()


class DecryptError(ValueError):
    """A stored value could not be decrypted with any configured key."""


def parse_keys(raw: str) -> list[bytes]:
    """``FIELD_ENCRYPTION_KEY`` -> Fernet keys (validated). Raises ``ValueError``."""
    keys = [k.strip().encode() for k in raw.split(",") if k.strip()]
    if not keys:
        raise ValueError("FIELD_ENCRYPTION_KEY is empty")
    for key in keys:
        Fernet(key)  # raises ValueError for a malformed key
    return keys


def normalize(kind: str, value: str) -> str:
    """The form a value is indexed in: emails are case-insensitive, phones are E.164."""
    value = value.strip()
    return value.lower() if kind == "email" else value


class FieldCrypto:
    def __init__(self, keys: Sequence[bytes], index_key: bytes) -> None:
        if not keys:
            raise ValueError("at least one encryption key is required")
        if len(index_key) < 16:
            raise ValueError("BLIND_INDEX_KEY must be at least 16 bytes")
        self._fernet = MultiFernet([Fernet(k) for k in keys])
        self._primary = Fernet(keys[0])
        self._index_key = index_key

    def encrypt(self, value: str) -> str:
        return self._primary.encrypt(value.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise DecryptError("value cannot be decrypted with the configured keys") from exc

    def rotate(self, token: str) -> str:
        """Re-encrypt ``token`` under the primary (first) key."""
        try:
            return self._fernet.rotate(token.encode()).decode()
        except InvalidToken as exc:
            raise DecryptError("value cannot be decrypted with the configured keys") from exc

    def blind_index(self, kind: str, value: str) -> str:
        message = f"{kind}|{normalize(kind, value)}".encode()
        return hmac.new(self._index_key, message, hashlib.sha256).hexdigest()


_active: FieldCrypto | None = None


def from_settings(settings: Settings) -> FieldCrypto:
    assert settings.field_encryption_key is not None  # the settings validator fills it
    assert settings.blind_index_key is not None
    return FieldCrypto(
        parse_keys(settings.field_encryption_key.get_secret_value()),
        settings.blind_index_key.get_secret_value().encode(),
    )


def configure(crypto: FieldCrypto) -> FieldCrypto:
    """Install the process-wide crypto (the app factory and the worker call this)."""
    global _active
    _active = crypto
    return crypto


def active() -> FieldCrypto:
    """The configured crypto; built from the environment on first use if nobody set it."""
    global _active
    if _active is None:
        from app.config import get_settings

        _active = from_settings(get_settings())
    return _active


def encrypt_optional(value: str | None) -> str | None:
    return active().encrypt(value) if value else None


def decrypt_optional(token: str | None) -> str | None:
    return active().decrypt(token) if token else None


def index_optional(kind: str, value: str | None) -> str | None:
    return active().blind_index(kind, value) if value else None
