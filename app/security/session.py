"""Dashboard sessions: a signed, expiring cookie. **Temporary** until real users and roles.

Login exchanges a token (``ADMIN_TOKEN``, or ``DEMO_TOKEN`` in demo mode) for a cookie
``<role>.<expires>.<token fingerprint>.<nonce>.<hmac>``:

- the HMAC (SHA-256, server ``SECRET_KEY``, domain-separated from CSRF tokens) means it
  cannot be forged or edited;
- ``expires`` bounds its life (``SESSION_TTL_HOURS``);
- the fingerprint ties it to the token that was used, so rotating ``ADMIN_TOKEN`` (or the
  demo token) logs every existing session out.

The cookie is HttpOnly, SameSite=Strict, and Secure everywhere except plain-HTTP
localhost development.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Literal

SESSION_COOKIE = "stl_session"
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
Role = Literal["admin", "demo"]
ROLES: tuple[Role, ...] = ("admin", "demo")


def _mac(key: bytes, label: str, payload: str) -> str:
    return hmac.new(key, f"{label}|{payload}".encode(), hashlib.sha256).hexdigest()


def fingerprint(key: bytes, token: str) -> str:
    return _mac(key, "session-token", token)[:16]


@dataclass(frozen=True)
class DashboardSession:
    role: Role
    expires: int

    @property
    def actor(self) -> str:
        """Name used on the timeline for actions taken in this session."""
        return "dashboard-admin" if self.role == "admin" else "demo-visitor"


def issue_session(key: bytes, role: Role, token: str, *, now: float, ttl_seconds: float) -> str:
    payload = f"{role}.{int(now + ttl_seconds)}.{fingerprint(key, token)}.{secrets.token_hex(8)}"
    return f"{payload}.{_mac(key, 'session', payload)}"


def read_session(
    key: bytes, cookie: str | None, tokens: dict[Role, str | None], *, now: float
) -> DashboardSession | None:
    """The session in ``cookie`` if it is genuine, unexpired and its token is still current."""
    if not cookie or cookie.count(".") != 4:
        return None
    role, expires_s, fp, nonce, mac = cookie.split(".")
    if not hmac.compare_digest(_mac(key, "session", f"{role}.{expires_s}.{fp}.{nonce}"), mac):
        return None
    matched = next((r for r in ROLES if r == role), None)
    if matched is None or not expires_s.isdigit() or int(expires_s) <= now:
        return None
    current = tokens.get(matched)
    if not current or not hmac.compare_digest(fingerprint(key, current), fp):
        return None
    return DashboardSession(role=matched, expires=int(expires_s))


def match_token(given: str, tokens: dict[Role, str | None]) -> Role | None:
    """Which role ``given`` logs in as (constant-time compare against each token)."""
    found: Role | None = None
    for role, expected in tokens.items():
        if expected and hmac.compare_digest(given.encode(), expected.encode()):
            found = found or role
    return found


def cookie_secure(hostname: str | None) -> bool:
    return (hostname or "") not in LOCAL_HOSTS
