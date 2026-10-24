import json
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.main import create_app
from app.models import Activity, Lead, WebhookEvent
from app.security.signatures import sign_body
from tests.conftest import META_SECRET, META_VERIFY, drain, make_settings


def _post(client: TestClient, payload: Any, *, signature: str | None = "auto") -> Any:
    body = json.dumps(payload).encode()
    headers = {"content-type": "application/json"}
    if signature == "auto":
        headers["x-hub-signature-256"] = sign_body(META_SECRET.encode(), body)
    elif signature is not None:
        headers["x-hub-signature-256"] = signature
    return client.post("/webhooks/meta", content=body, headers=headers)


def _count(client: TestClient, model: Any) -> int:
    with client.app.state.session_factory() as s:  # type: ignore[attr-defined]
        return int(s.scalar(select(func.count()).select_from(model)) or 0)


def test_verification_handshake(client: TestClient) -> None:
    ok = client.get(
        "/webhooks/meta",
        params={"hub.mode": "subscribe", "hub.verify_token": META_VERIFY, "hub.challenge": "1158"},
    )
    assert ok.status_code == 200
    assert ok.text == "1158"


def test_verification_rejects_bad_token_or_mode(client: TestClient) -> None:
    bad = {"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "1"}
    assert client.get("/webhooks/meta", params=bad).status_code == 403
    bad_mode = {"hub.mode": "unsubscribe", "hub.verify_token": META_VERIFY, "hub.challenge": "1"}
    assert client.get("/webhooks/meta", params=bad_mode).status_code == 403
    xss = {"hub.mode": "subscribe", "hub.verify_token": META_VERIFY, "hub.challenge": "<b>"}
    assert client.get("/webhooks/meta", params=xss).status_code == 403
    assert client.get("/webhooks/meta").status_code == 403


def test_good_signature_creates_lead(client: TestClient, meta_payload: dict[str, Any]) -> None:
    r = _post(client, meta_payload)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "received": 1,
        "created": 1,
        "duplicate": 0,
        "rejected": 0,
        "needs_fetch": 0,
    }
    drain(client)
    with client.app.state.session_factory() as s:  # type: ignore[attr-defined]
        lead = s.scalars(select(Lead)).one()
        assert lead.source == "meta_ads"
        assert lead.phone == "+12025550147"
        assert lead.scored_by == "mock"
        assert lead.next_step == "call_now"  # roof leak + phone
        created = s.scalars(select(Activity).where(Activity.kind == "lead_created")).one()
        assert created.actor == "webhook:meta"
        assert created.detail["leadgen_id"] == "900000000000001"


def test_missing_signature_is_401(client: TestClient, meta_payload: dict[str, Any]) -> None:
    assert _post(client, meta_payload, signature=None).status_code == 401
    assert _count(client, Lead) == 0


def test_bad_signature_is_401(client: TestClient, meta_payload: dict[str, Any]) -> None:
    assert _post(client, meta_payload, signature="sha256=" + "0" * 64).status_code == 401
    assert _post(client, meta_payload, signature="garbage").status_code == 401


def test_tampered_body_is_401(client: TestClient, meta_payload: dict[str, Any]) -> None:
    body = json.dumps(meta_payload).encode()
    sig = sign_body(META_SECRET.encode(), body)
    tampered = body.replace(b"Dana", b"Evil")
    r = client.post("/webhooks/meta", content=tampered, headers={"x-hub-signature-256": sig})
    assert r.status_code == 401


def test_duplicate_delivery_is_idempotent(client: TestClient, meta_payload: dict[str, Any]) -> None:
    assert _post(client, meta_payload).json()["created"] == 1
    second = _post(client, meta_payload)
    assert second.status_code == 200
    assert second.json()["duplicate"] == 1
    assert second.json()["created"] == 0
    assert _count(client, Lead) == 1
    assert _count(client, WebhookEvent) == 1


def test_needs_fetch_and_rejected(client: TestClient, meta_payload: dict[str, Any]) -> None:
    change = meta_payload["entry"][0]["changes"][0]
    no_fields = json.loads(json.dumps(change))
    no_fields["value"]["leadgen_id"] = "900000000000002"
    del no_fields["value"]["field_data"]
    junk = json.loads(json.dumps(change))
    junk["value"]["leadgen_id"] = "900000000000003"
    junk["value"]["field_data"] = [{"name": "full_name", "values": ["www.spam.example"]}]
    meta_payload["entry"][0]["changes"] = [no_fields, junk]
    r = _post(client, meta_payload)
    assert r.status_code == 200
    assert r.json()["needs_fetch"] == 1
    assert r.json()["rejected"] == 1
    assert _count(client, Lead) == 0
    with client.app.state.session_factory() as s:  # type: ignore[attr-defined]
        statuses = sorted(e.status for e in s.scalars(select(WebhookEvent)))
    assert statuses == ["needs_fetch", "rejected"]


def test_malformed_payload_is_400(client: TestClient) -> None:
    body = b"{not json"
    headers = {"x-hub-signature-256": sign_body(META_SECRET.encode(), body)}
    assert client.post("/webhooks/meta", content=body, headers=headers).status_code == 400
    assert _post(client, {"object": "user"}).status_code == 400


def test_oversized_payload_is_413(client: TestClient) -> None:
    body = b"x" * (256 * 1024 + 1)
    headers = {"x-hub-signature-256": sign_body(META_SECRET.encode(), body)}
    assert client.post("/webhooks/meta", content=body, headers=headers).status_code == 413


def test_unconfigured_webhook_fails_closed(meta_payload: dict[str, Any]) -> None:
    app = create_app(make_settings(meta_app_secret=None, meta_verify_token=None))
    with TestClient(app) as c:
        assert _post(c, meta_payload).status_code == 503
        params = {"hub.mode": "subscribe", "hub.verify_token": "", "hub.challenge": "1"}
        assert c.get("/webhooks/meta", params=params).status_code == 403
