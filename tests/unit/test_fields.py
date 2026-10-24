"""Field-level encryption, blind index, key rotation and the settings that configure them."""

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr, ValidationError
from sqlalchemy import select, text

from app.db import init_db, make_engine, make_session_factory
from app.models import Lead, OutboxMessage
from app.security import fields
from app.security.fields import DecryptError, FieldCrypto, dev_key, parse_keys
from tests.conftest import make_settings

INDEX_KEY = b"0123456789abcdef-index"


def test_round_trip_random_iv_and_blind_index() -> None:
    crypto = FieldCrypto([Fernet.generate_key()], INDEX_KEY)
    a, b = crypto.encrypt("+12025550123"), crypto.encrypt("+12025550123")
    assert a != b  # random IV: equal values do not look equal at rest
    assert "5550123" not in a
    assert crypto.decrypt(a) == crypto.decrypt(b) == "+12025550123"
    # The index is deterministic, normalized and domain-separated by kind.
    assert crypto.blind_index("email", "Ana@Example.com ") == crypto.blind_index(
        "email", "ana@example.com"
    )
    assert crypto.blind_index("phone", "x@example.com") != crypto.blind_index(
        "email", "x@example.com"
    )
    other = FieldCrypto([Fernet.generate_key()], b"another-index-key-000")
    assert other.blind_index("phone", "+1") != crypto.blind_index("phone", "+1")
    with pytest.raises(DecryptError):
        other.decrypt(a)
    with pytest.raises(DecryptError):
        other.rotate(a)


def test_key_rotation_keeps_old_values_readable() -> None:
    old, new = Fernet.generate_key(), Fernet.generate_key()
    before = FieldCrypto([old], INDEX_KEY)
    token = before.encrypt("lee@example.com")
    after = FieldCrypto([new, old], INDEX_KEY)  # new key first, old still listed
    assert after.decrypt(token) == "lee@example.com"
    rotated = after.rotate(token)
    assert FieldCrypto([new], INDEX_KEY).decrypt(rotated) == "lee@example.com"
    with pytest.raises(DecryptError):
        before.decrypt(rotated)


def test_bad_keys_are_rejected() -> None:
    with pytest.raises(ValueError, match="empty"):
        parse_keys(" , ")
    with pytest.raises(ValueError, match="32 url-safe"):
        parse_keys("not-a-fernet-key")
    with pytest.raises(ValueError, match="at least one"):
        FieldCrypto([], INDEX_KEY)
    with pytest.raises(ValueError, match="16 bytes"):
        FieldCrypto([Fernet.generate_key()], b"short")


def test_settings_require_keys_in_prod_and_validate_them() -> None:
    with pytest.raises(ValidationError, match="FIELD_ENCRYPTION_KEY must be set"):
        make_settings(environment="prod", field_encryption_key=None)
    with pytest.raises(ValidationError, match="Fernet keys"):
        make_settings(field_encryption_key=SecretStr("nope"))
    with pytest.raises(ValidationError, match="BLIND_INDEX_KEY"):
        make_settings(blind_index_key=SecretStr("short"))
    s = make_settings(field_encryption_key=None, blind_index_key=None)
    assert s.uses_dev_field_keys  # the public dev fallback
    assert s.field_encryption_key is not None
    assert s.field_encryption_key.get_secret_value() == dev_key("field-encryption")
    real = make_settings(
        field_encryption_key=SecretStr(Fernet.generate_key().decode()),
        blind_index_key=SecretStr("x" * 32),
    )
    assert not real.uses_dev_field_keys
    demo = make_settings(demo_mode=True)
    assert not demo.uses_dev_field_keys  # demo mode gets random per-process keys


def test_rows_hold_ciphertext_only() -> None:
    engine = make_engine("sqlite://")
    init_db(engine)
    with make_session_factory(engine)() as s:
        lead = Lead(name="Sam Ortiz", phone="+12025550188", email="sam@example.com", source="api")
        s.add(lead)
        s.flush()
        s.add(
            OutboxMessage(
                lead_id=lead.id,
                step="ack_sms",
                channel="sms",
                to_address="+12025550188",
                body="hi",
                sender="MOCK-sms",
                idempotency_key="k",
            )
        )
        s.commit()
        raw = " ".join(
            str(v)
            for row in s.execute(text("SELECT * FROM leads")).all()
            + s.execute(text("SELECT * FROM outbox_messages")).all()
            for v in row
        )
        assert "5550188" not in raw
        assert "sam@example.com" not in raw
        found = s.scalars(select(Lead).where(Lead.phone_is("+12025550188"))).one()
        assert found.email == "sam@example.com"
        assert s.scalars(select(Lead).where(Lead.email_is("SAM@example.com"))).one() is found
        assert s.scalars(select(OutboxMessage)).one().to_address == "+12025550188"
        found.phone = None
        assert found.phone is None
        assert found.phone_bidx is None


def test_active_builds_from_settings_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fields, "_active", None)
    assert fields.active().decrypt(fields.active().encrypt("x")) == "x"
