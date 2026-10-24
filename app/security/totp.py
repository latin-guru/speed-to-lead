"""TOTP (RFC 6238) for two-factor login, with replay protection and recovery codes.

- 6-digit codes, 30-second steps, SHA-1 (what every authenticator app supports). A code
  from the previous or next step is accepted to allow for clock drift, but a step can only
  be used once per user (``last_step``), so a code seen over someone's shoulder cannot be
  replayed.
- The enrollment QR code is drawn on the server (segno, SVG) and embedded as a ``data:``
  URI: the secret never goes to a third-party QR service.
- Recovery codes are 10 random codes of 10 base32 characters (50 bits each), shown once.
  Only their SHA-256 is stored; each works once.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import datetime

import pyotp
import segno

DIGITS = 6
PERIOD = 30
DRIFT_STEPS = 1
RECOVERY_CODES = 10
_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"  # no 0/o, 1/l  # pragma: allowlist secret


def new_secret() -> str:
    return pyotp.random_base32()


def demo_secret() -> str:
    """The public demo admin's secret, derived from a public string (fake data only)."""
    digest = hashlib.sha256(b"speed-to-lead public demo totp secret").digest()[:20]
    return base64.b32encode(digest).decode()


def _totp(secret: str) -> pyotp.TOTP:
    return pyotp.TOTP(secret, digits=DIGITS, interval=PERIOD)


def provisioning_uri(secret: str, *, account: str, issuer: str) -> str:
    return _totp(secret).provisioning_uri(name=account, issuer_name=issuer)


def qr_svg_data_uri(uri: str) -> str:
    return str(segno.make(uri, error="m").svg_data_uri(scale=5, border=2))


def current_code(secret: str, now: datetime) -> str:
    return str(_totp(secret).at(now))


def step_of(now: datetime) -> int:
    return int(now.timestamp()) // PERIOD


def verify(secret: str, code: str, *, now: datetime, last_step: int | None) -> int | None:
    """The matching time step if ``code`` is valid and not replayed, else None."""
    code = code.strip().replace(" ", "")
    if len(code) != DIGITS or not code.isdigit():
        return None
    totp = _totp(secret)
    current = step_of(now)
    matched = None
    for step in range(current - DRIFT_STEPS, current + DRIFT_STEPS + 1):
        if hmac.compare_digest(totp.generate_otp(step), code):
            matched = step
    if matched is None or (last_step is not None and matched <= last_step):
        return None
    return matched


def normalize_recovery(code: str) -> str:
    return "".join(ch for ch in code.lower() if ch.isalnum())


def hash_recovery(code: str) -> str:
    return hashlib.sha256(f"recovery|{normalize_recovery(code)}".encode()).hexdigest()


def new_recovery_codes(count: int = RECOVERY_CODES) -> list[str]:
    codes = []
    for _ in range(count):
        raw = "".join(secrets.choice(_ALPHABET) for _ in range(10))
        codes.append(f"{raw[:5]}-{raw[5:]}")
    return codes


def looks_like_recovery_code(code: str) -> bool:
    return len(normalize_recovery(code)) == 10
