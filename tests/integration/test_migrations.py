"""Alembic owns the schema: migrations must match the models, and real runs must use them."""

from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from app.db import ROOT, Base, alembic_head, current_revision, make_engine, schema_is_current
from app.main import create_app
from app.worker import main as worker_main
from tests.conftest import make_settings


def _cfg(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.attributes["configure_logger"] = False
    return cfg


def test_migrations_match_models_and_downgrade(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'm.db'}"
    command.upgrade(_cfg(url), "head")
    engine = make_engine(url)
    try:
        assert current_revision(engine) == alembic_head() == "0006"
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn, opts={"compare_type": True})
            assert compare_metadata(ctx, Base.metadata) == []
        command.downgrade(_cfg(url), "0001")
        assert "jobs" not in inspect(engine).get_table_names()
        assert "timezone" not in {c["name"] for c in inspect(engine).get_columns("leads")}
        command.downgrade(_cfg(url), "base")
        assert inspect(engine).get_table_names() == ["alembic_version"]
    finally:
        engine.dispose()


def test_app_outside_tests_requires_migrated_schema(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'app.db'}"
    settings = make_settings(environment="dev", database_url=url)

    with TestClient(create_app(settings)) as c:  # no create_all outside tests
        r = c.get("/readyz")
        assert r.status_code == 503
        assert r.json()["checks"]["schema"] is False
    assert worker_main(["--drain"], settings=settings) == 2  # worker refuses too
    assert not schema_is_current(make_engine(url))

    command.upgrade(_cfg(url), "head")
    with TestClient(create_app(settings)) as c:
        assert c.get("/readyz").json()["checks"]["schema"] is True
        assert (
            c.post("/api/leads", json={"name": "Lee Park", "phone": "202-555-0142"}).status_code
            == 201
        )
    assert worker_main(["--drain"], settings=settings) == 0


def test_schema_check_handles_unreachable_db() -> None:
    assert not schema_is_current(make_engine("sqlite:////nonexistent-dir/x.db"))


def test_contact_encryption_migration_backfills_and_reverses(tmp_path: Path) -> None:
    """0003 encrypts existing plaintext rows (and the downgrade decrypts them back)."""
    import uuid

    from sqlalchemy import text

    from app.security import fields

    url = f"sqlite:///{tmp_path / 'b.db'}"
    command.upgrade(_cfg(url), "0002")
    engine = make_engine(url)
    lead_id = uuid.uuid4()
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO leads (id, name, email, phone, source, stage, sequence_state,"
                    " created_at) VALUES (:id, 'Ana Ruiz', 'ana@example.com', '+12025550161',"
                    " 'api', 'new', 'active', '2026-03-02 15:00:00')"
                ),
                {"id": lead_id.hex},
            )
            conn.execute(
                text(
                    "INSERT INTO leads (id, name, source, stage, sequence_state, created_at)"
                    " VALUES (:id, 'No Contact', 'api', 'new', 'active', '2026-03-02 15:00:00')"
                ),
                {"id": uuid.uuid4().hex},
            )
            conn.execute(
                text(
                    "INSERT INTO outbox_messages (lead_id, step, channel, to_address, body,"
                    " sender, idempotency_key, transactional, sent_at) VALUES (:id, 'ack_sms',"
                    " 'sms', '+12025550161', 'hi', 'MOCK-sms', 'k1', 1, '2026-03-02 15:00:01')"
                ),
                {"id": lead_id.hex},
            )
        command.upgrade(_cfg(url), "head")
        crypto = fields.active()
        with engine.connect() as conn:
            phone_enc, phone_bidx, email_enc = conn.execute(
                text("SELECT phone_enc, phone_bidx, email_enc FROM leads WHERE name='Ana Ruiz'")
            ).one()
            assert "5550161" not in phone_enc
            assert crypto.decrypt(phone_enc) == "+12025550161"
            assert crypto.decrypt(email_enc) == "ana@example.com"
            assert phone_bidx == crypto.blind_index("phone", "+12025550161")
            assert (
                conn.execute(text("SELECT phone_enc FROM leads WHERE name='No Contact'")).scalar()
                is None
            )
            to_enc = conn.execute(text("SELECT to_address_enc FROM outbox_messages")).scalar()
            assert crypto.decrypt(to_enc) == "+12025550161"
        assert "phone" not in {c["name"] for c in inspect(engine).get_columns("leads")}
        command.downgrade(_cfg(url), "0002")
        with engine.connect() as conn:
            assert conn.execute(
                text("SELECT phone, email FROM leads WHERE name='Ana Ruiz'")
            ).one() == ("+12025550161", "ana@example.com")
            assert (
                conn.execute(text("SELECT to_address FROM outbox_messages")).scalar()
                == "+12025550161"
            )
    finally:
        engine.dispose()
