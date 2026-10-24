"""Stateless CSRF tokens (signed double-submit cookie).

The same token is set in an HttpOnly cookie and embedded in the form (or sent as the
``X-CSRF-Token`` header by a script). The token is ``<issued_at>.<nonce>.<hmac>``, and the
HMAC also covers a *binding*: the logged-in session (a hash of its id) on dashboard pages,
empty on the public lead form and the login page. So a token cannot be forged without the
server key, it expires, and a token from one session (or from before login) is useless in
another. SameSite=Strict cookies and an Origin check on dashboard POSTs back this up.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

CSRF_COOKIE = "stl_csrf"
CSRF_FIELD = "csrf_token"
CSRF_HEADER = "x-csrf-token"
MAX_AGE_SECONDS = 2 * 60 * 60


def _mac(key: bytes, payload: str, binding: str) -> str:
    return hmac.new(key, f"csrf|{binding}|{payload}".encode(), hashlib.sha256).hexdigest()


def issue_token(key: bytes, *, binding: str = "", now: float | None = None) -> str:
    issued = int(now if now is not None else time.time())
    payload = f"{issued}.{secrets.token_urlsafe(16)}"
    return f"{payload}.{_mac(key, payload, binding)}"


def token_valid(
    key: bytes, token: str | None, *, binding: str = "", now: float | None = None
) -> bool:
    if not token or token.count(".") != 2 or len(token) > 200:
        return False
    issued_s, nonce, mac = token.split(".")
    payload = f"{issued_s}.{nonce}"
    if not hmac.compare_digest(_mac(key, payload, binding), mac):
        return False
    try:
        issued = int(issued_s)
    except ValueError:
        return False
    age = (now if now is not None else time.time()) - issued
    return 0 <= age <= MAX_AGE_SECONDS


def check_double_submit(
    key: bytes, cookie_token: str | None, form_token: str | None, *, binding: str = ""
) -> bool:
    if not cookie_token or not form_token:
        return False
    if not hmac.compare_digest(cookie_token.encode(), form_token.encode()):
        return False
    return token_valid(key, form_token, binding=binding)
