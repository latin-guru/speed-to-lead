"""HTTP hardening middleware (pure ASGI, so it also covers errors and static files).

``SecurityHeadersMiddleware`` adds, unless a route already set them:

- ``Content-Security-Policy``: scripts, styles, images, fonts and fetches from this origin
  only; no inline script (the dashboard has none: its JS is in static files), no plugins,
  no ``<base>`` changes, forms post only here, and the page cannot be framed.
- ``X-Content-Type-Options: nosniff``, ``X-Frame-Options: DENY`` (for old browsers),
  ``Referrer-Policy: same-origin`` (lead ids in URLs never leak to other sites; same-origin
  POSTs keep a real Origin header for the CSRF check), a ``Permissions-Policy`` that turns
  off camera, microphone, location and similar, and ``Cross-Origin-Opener-Policy`` /
  ``Cross-Origin-Resource-Policy: same-origin``.
- ``Cache-Control: no-store`` on everything except ``/static`` (pages and API answers can
  hold lead data).
- ``Strict-Transport-Security`` on HTTPS responses (``HSTS_ENABLED``).

``BodySizeLimitMiddleware`` rejects request bodies over ``MAX_REQUEST_BYTES`` with 413,
both when ``Content-Length`` says so and when a chunked body grows past the limit.

The API docs page (``/docs``) is the one exception to the CSP: Swagger UI loads from a CDN
and needs one inline script, which gets a per-request nonce (``docs_csp``).
"""

from __future__ import annotations

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; manifest-src 'self'; form-action 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; object-src 'none'"
)
PERMISSIONS_POLICY = (
    "accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), "
    "microphone=(), payment=(), usb=(), browsing-topics=()"
)
HSTS = "max-age=31536000; includeSubDomains"
DOCS_CDN = "https://cdn.jsdelivr.net"


def docs_csp(nonce: str) -> str:
    return (
        f"default-src 'none'; script-src 'nonce-{nonce}' {DOCS_CDN}; "
        f"style-src {DOCS_CDN} 'unsafe-inline'; img-src 'self' data: https://fastapi.tiangolo.com; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'; "
        "form-action 'none'"
    )


def _is_https(scope: Scope, trust_forwarded: bool) -> bool:
    if scope.get("scheme") == "https":
        return True
    if trust_forwarded:
        proto = Headers(scope=scope).get("x-forwarded-proto", "")
        return proto.split(",")[-1].strip().lower() == "https"
    return False


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, *, hsts: bool = True, trust_forwarded: bool = False) -> None:
        self.app = app
        self.hsts = hsts
        self.trust_forwarded = trust_forwarded

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        https = self.hsts and _is_https(scope, self.trust_forwarded)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                defaults = {
                    "Content-Security-Policy": CSP,
                    "X-Content-Type-Options": "nosniff",
                    "X-Frame-Options": "DENY",
                    "Referrer-Policy": "same-origin",
                    "Permissions-Policy": PERMISSIONS_POLICY,
                    "Cross-Origin-Opener-Policy": "same-origin",
                    "Cross-Origin-Resource-Policy": "same-origin",
                }
                if not path.startswith("/static/"):
                    defaults["Cache-Control"] = "no-store"
                if https:
                    defaults["Strict-Transport-Security"] = HSTS
                for name, value in defaults.items():
                    if name not in headers:
                        headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)


class _BodyTooLargeError(Exception):
    pass


BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def _reject(self, send: Send) -> None:
        body = b'{"detail":"request body too large"}'
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") not in BODY_METHODS:
            await self.app(scope, receive, send)
            return
        declared = Headers(scope=scope).get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > self.max_bytes):
            await self._reject(send)
            return
        received = 0
        started = False

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _BodyTooLargeError
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except _BodyTooLargeError:
            if not started:
                await self._reject(send)
