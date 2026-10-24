import re

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models import Lead
from app.security.csrf import CSRF_COOKIE


def _token(client: TestClient) -> str:
    page = client.get("/")
    assert page.status_code == 200
    assert CSRF_COOKIE in page.cookies
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert match
    return match.group(1)


def _lead_count(client: TestClient) -> int:
    with client.app.state.session_factory() as s:  # type: ignore[attr-defined]
        return int(s.scalar(select(func.count(Lead.id))) or 0)


FORM = {
    "name": "Riley Chen",
    "phone": "202-555-0166",
    "email": "",
    "message": "Interested in solar panels and a battery.",
}


def test_form_page_renders(client: TestClient) -> None:
    page = client.get("/")
    assert "Copperline Roofing" in page.text
    assert 'name="website"' in page.text  # honeypot present


def test_form_submit_creates_lead(client: TestClient) -> None:
    token = _token(client)
    r = client.post("/", data={**FORM, "csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/thanks"
    assert _lead_count(client) == 1
    assert client.get("/thanks").status_code == 200


def test_form_requires_csrf(client: TestClient) -> None:
    _token(client)
    r = client.post("/", data={**FORM, "csrf_token": "forged.token.value"})
    assert r.status_code == 403
    assert _lead_count(client) == 0


def test_form_rejects_missing_cookie(client: TestClient) -> None:
    token = _token(client)
    client.cookies.clear()
    r = client.post("/", data={**FORM, "csrf_token": token})
    assert r.status_code == 403


def test_honeypot_silently_drops(client: TestClient) -> None:
    token = _token(client)
    r = client.post(
        "/", data={**FORM, "csrf_token": token, "website": "spam"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert _lead_count(client) == 0


def test_invalid_form_rerenders_with_errors(client: TestClient) -> None:
    token = _token(client)
    r = client.post("/", data={**FORM, "name": "http://x.example", "csrf_token": token})
    assert r.status_code == 422
    assert "name" in r.text
    assert 'value="202-555-0166"' in r.text  # values preserved
    assert _lead_count(client) == 0


def test_form_phone_cap(client: TestClient) -> None:
    for _ in range(3):
        token = _token(client)
        client.post("/", data={**FORM, "csrf_token": token}, follow_redirects=False)
    token = _token(client)
    r = client.post("/", data={**FORM, "csrf_token": token})
    assert r.status_code == 429
    assert "Retry-After" in r.headers
