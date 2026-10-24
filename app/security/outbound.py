"""Outbound HTTP allowlist (SSRF guard).

The app only ever calls a few third-party APIs, and every URL it calls is built from
configuration, never from request data: lead fields, webhook payloads and dashboard forms
are not used to pick a host. On top of that, every base URL must pass ``check_url`` at
startup (settings validation) and again when a client is built:

- ``https`` only, no user-info, no non-default port, no path, query or fragment;
- the host must be one of ``ALLOWED_HOSTS`` exactly, or a Salesforce My Domain host
  (``<name>.my.salesforce.com``, sandbox/scratch variants included).

So a misconfiguration or a tampered environment cannot point the app at internal
addresses (for example a cloud metadata service) or an attacker's server.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

ALLOWED_HOSTS = frozenset(
    {
        "api.anthropic.com",  # Claude scorer
        "api.x.ai",  # Grok scorer
        "api.hubapi.com",  # HubSpot
        "services.leadconnectorhq.com",  # GoHighLevel (API v2)
        "oauth2.googleapis.com",  # Gmail API skeleton (interview email): token refresh
        "gmail.googleapis.com",  # Gmail API skeleton (interview email): messages.send
    }
)
_SALESFORCE = re.compile(
    r"^[a-z0-9][a-z0-9-]{0,62}(\.(sandbox|develop|scratch))?\.my\.salesforce\.com$"
)


class OutboundURLError(ValueError):
    pass


def check_url(url: str) -> str:
    """Return ``url`` (without a trailing slash) if it is an allowed API base URL."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError as exc:
        raise OutboundURLError(f"not a valid URL: {url!r}") from exc
    host = (parts.hostname or "").lower()
    problems = []
    if parts.scheme != "https":
        problems.append("must use https")
    if parts.username or parts.password:
        problems.append("must not contain credentials")
    if port not in (None, 443):
        problems.append("must use the default port")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        problems.append("must be a bare origin (no path or query)")
    if host not in ALLOWED_HOSTS and not _SALESFORCE.match(host):
        problems.append(f"host {host or '(none)'} is not on the outbound allowlist")
    if problems:
        raise OutboundURLError(f"outbound URL {url!r} rejected: " + ", ".join(problems))
    return f"https://{host}"
