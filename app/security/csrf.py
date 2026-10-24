"""Stateless CSRF tokens for the server-rendered form.

Double-submit pattern with a signed token: the same token is set in an HttpOnly cookie and
embedded in the form. The token is ``<issued_at>.<nonce>.<hmac>`` so it cannot be forged
without the server key and it expires.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

CSRF_COOKIE = "stl_csrf"
CSRF_FIELD = "csrf_token"
MAX_AGE_SECONDS = 2 * 60 * 60


def _mac(key: bytes, payload: str) -> str:
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def issue_token(key: bytes, *, now: float | None = None) -> str:
    issued = int(now if now is not None else time.time())
    payload = f"{issued}.{secrets.token_urlsafe(16)}"
    return f"{payload}.{_mac(key, payload)}"


def token_valid(key: bytes, token: str | None, *, now: float | None = None) -> bool:
    if not token or token.count(".") != 2:
        return False
    issued_s, nonce, mac = token.split(".")
    payload = f"{issued_s}.{nonce}"
    if not hmac.compare_digest(_mac(key, payload), mac):
        return False
    try:
        issued = int(issued_s)
    except ValueError:
        return False
    age = (now if now is not None else time.time()) - issued
    return 0 <= age <= MAX_AGE_SECONDS


def check_double_submit(key: bytes, cookie_token: str | None, form_token: str | None) -> bool:
    if not cookie_token or not form_token:
        return False
    if not hmac.compare_digest(cookie_token.encode(), form_token.encode()):
        return False
    return token_valid(key, form_token)
