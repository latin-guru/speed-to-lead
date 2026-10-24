"""``python -m app.cli`` commands."""

import io
from pathlib import Path

from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import select

from app import cli
from app.db import init_db, make_engine, make_session_factory
from app.models import Lead, OutboxMessage
from app.security import fields
from tests.conftest import make_settings

INDEX = SecretStr("cli-test-blind-index-key")


def _db(tmp_path: Path) -> str:
    url = f"sqlite:///{tmp_path / 'cli.db'}"
    engine = make_engine(url)
    init_db(engine)
    engine.dispose()
    return url


def test_generate_key_prints_a_fernet_key() -> None:
    out = io.StringIO()
    assert cli.main(["generate-key"], out=out) == 0
    Fernet(out.getvalue().strip().encode())  # valid


def test_rotate_keys_re_encrypts_with_the_new_key(tmp_path: Path) -> None:
    url = _db(tmp_path)
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    before = make_settings(
        database_url=url, field_encryption_key=SecretStr(old), blind_index_key=INDEX
    )
    fields.configure(fields.from_settings(before))
    engine = make_engine(url)
    with make_session_factory(engine)() as s:
        lead = Lead(name="Kai Moss", phone="+12025550190", email="kai@example.com", source="api")
        no_contact = Lead(name="No Contact", source="api")
        s.add_all([lead, no_contact])
        s.flush()
        s.add(
            OutboxMessage(
                lead_id=lead.id,
                step="ack_sms",
                channel="sms",
                to_address="+12025550190",
                body="hi",
                sender="MOCK-sms",
                idempotency_key="k",
            )
        )
        s.commit()

    rotated = make_settings(
        database_url=url, field_encryption_key=SecretStr(f"{new},{old}"), blind_index_key=INDEX
    )
    out = io.StringIO()
    assert cli.main(["rotate-keys"], settings=rotated, out=out) == 0
    assert "re-encrypted 3 values" in out.getvalue()

    only_new = make_settings(
        database_url=url, field_encryption_key=SecretStr(new), blind_index_key=INDEX
    )
    fields.configure(fields.from_settings(only_new))
    with make_session_factory(engine)() as s:
        found = s.scalars(select(Lead).where(Lead.phone_is("+12025550190"))).one()
        assert found.email == "kai@example.com"
        assert s.scalars(select(OutboxMessage)).one().to_address == "+12025550190"
    engine.dispose()
