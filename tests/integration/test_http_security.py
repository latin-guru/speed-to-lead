"""Security headers, the CSP, CORS, request size limits and the API docs page."""

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.models import UserRole
from tests.conftest import make_settings, valid_lead
from tests.web_helpers import login_as, make_client, post, seeded

TEMPLATES = Path(__file__).resolve().parents[2] / "app" / "web" / "templates"


@pytest.fixture
def web() -> Iterator[TestClient]:
    yield from make_client()


def _assert_hardened(r: object, *, cache: bool = True) -> None:
    h = r.headers  # type: ignore[attr-defined]
    csp = h["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "script-src 'self'" in csp
    assert "unsafe-inline" not in csp
    assert "frame-ancestors 'none'" in csp
    assert h["x-content-type-options"] == "nosniff"
    assert h["x-frame-options"] == "DENY"
    assert h["referrer-policy"] == "same-origin"
    assert "camera=()" in h["permissions-policy"]
    assert h["cross-origin-opener-policy"] == "same-origin"
    if cache:
        assert h["cache-control"] == "no-store"


def test_every_kind_of_response_is_hardened(web: TestClient) -> None:
    _assert_hardened(web.get("/login"))
    _assert_hardened(web.get("/"))
    _assert_hardened(web.get("/healthz"))
    _assert_hardened(web.get("/nope"))  # 404
    _assert_hardened(web.post("/api/leads", json=valid_lead()))
    _assert_hardened(web.get("/dashboard"))  # the redirect to /login
    static = web.get("/static/dashboard.css")
    _assert_hardened(static, cache=False)
    assert "no-store" not in static.headers.get("cache-control", "")
    assert "strict-transport-security" not in web.get("/login").headers  # plain HTTP


def test_hsts_on_https_and_behind_a_trusted_proxy() -> None:
    for c in make_client(base_url="https://dash.example.com"):
        assert c.get("/login").headers["strict-transport-security"].startswith("max-age=31536000")
    for c in make_client(trust_forwarded_for=True):
        r = c.get("/login", headers={"X-Forwarded-Proto": "https"})
        assert "strict-transport-security" in r.headers
    for c in make_client():  # an untrusted X-Forwarded-Proto is ignored
        r = c.get("/login", headers={"X-Forwarded-Proto": "https"})
        assert "strict-transport-security" not in r.headers
    for c in make_client(base_url="https://dash.example.com", hsts_enabled=False):
        assert "strict-transport-security" not in c.get("/login").headers


def test_revealed_contact_page_is_not_cached(web: TestClient) -> None:
    lead_id = seeded(web, 1)[0]
    login_as(web, UserRole.ADMIN)
    r = post(web, f"/dashboard/leads/{lead_id}/reveal")
    assert r.status_code == 200
    _assert_hardened(r)


def test_templates_have_no_inline_script_or_style() -> None:
    for path in TEMPLATES.glob("*.html"):
        text = path.read_text()
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", text), path.name
        assert "<style" not in text, path.name
        assert not re.search(r"\sstyle=", text), path.name
        assert not re.search(r"\son[a-z]+=", text), path.name  # no inline event handlers
        assert "javascript:" not in text, path.name


def test_docs_page_uses_a_nonce_for_its_one_inline_script(web: TestClient) -> None:
    r = web.get("/docs")
    assert r.status_code == 200
    csp = r.headers["content-security-policy"]
    nonce = re.search(r"'nonce-([^']+)'", csp)
    assert nonce
    inline = re.findall(r"<script(?![^>]*\bsrc=)([^>]*)>", r.text)
    assert inline
    assert all(f'nonce="{nonce.group(1)}"' in attrs for attrs in inline)
    assert "'unsafe-inline'" not in csp.split("script-src", 1)[1].split(";", 1)[0]
    assert web.get("/docs").headers["content-security-policy"] != csp  # fresh per request
    assert web.get("/redoc").status_code == 404
    assert web.get("/openapi.json").status_code == 200


def test_docs_can_be_turned_off() -> None:
    for c in make_client(api_docs_enabled=False):
        assert c.get("/docs").status_code == 404
        assert c.get("/openapi.json").status_code == 404


# --------------------------------------------------------------------------- CORS


def _preflight(c: TestClient, origin: str) -> object:
    return c.options(
        "/api/leads",
        headers={"Origin": origin, "Access-Control-Request-Method": "POST"},
    )


def test_cors_is_off_by_default(web: TestClient) -> None:
    r = _preflight(web, "https://evil.example.net")
    assert "access-control-allow-origin" not in r.headers  # type: ignore[attr-defined]
    r2 = web.get("/healthz", headers={"Origin": "https://evil.example.net"})
    assert "access-control-allow-origin" not in r2.headers


def test_cors_allowlist_is_exact() -> None:
    for c in make_client(cors_allowed_origins="https://site.example.com, https://b.example.com/"):
        ok = _preflight(c, "https://site.example.com")
        assert ok.headers["access-control-allow-origin"] == "https://site.example.com"  # type: ignore[attr-defined]
        assert "access-control-allow-credentials" not in ok.headers  # type: ignore[attr-defined]
        assert _preflight(c, "https://b.example.com").status_code == 200  # type: ignore[attr-defined]
        for bad in ("https://evil.example.net", "https://site.example.com.evil.net", "null"):
            r = _preflight(c, bad)
            assert "access-control-allow-origin" not in r.headers  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "value", ["*", "site.example.com", "https://site.example.com/path", "ftp://x.example.com"]
)
def test_cors_config_rejects_wildcards_and_non_origins(value: str) -> None:
    with pytest.raises(ValidationError, match="CORS_ALLOWED_ORIGINS"):
        make_settings(cors_allowed_origins=value)


# --------------------------------------------------------------------------- size limits


def test_request_body_size_limit() -> None:
    for c in make_client(max_request_bytes=2048):
        big = {"name": "Lee Park", "phone": "202-555-0142", "message": "x" * 4000}
        r = c.post("/api/leads", json=big)
        assert r.status_code == 413
        _assert_hardened(r)
        assert c.post("/api/leads", json=valid_lead()).status_code == 201

        def chunks() -> Iterator[bytes]:  # no Content-Length: counted while streaming
            for _ in range(10):
                yield b"name=" + b"y" * 500 + b"&"

        r = c.post(
            "/",
            content=chunks(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert r.status_code == 413
        r = c.post("/api/leads", content=b"{}", headers={"Content-Length": "abc"})
        assert r.status_code == 413
        assert c.get("/healthz").status_code == 200  # GETs are not limited
