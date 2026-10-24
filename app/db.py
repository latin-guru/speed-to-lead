"""Database engine, session helpers and shared column types (SQLAlchemy 2.x, sync).

The schema is owned by Alembic (``migrations/``). ``init_db`` (``create_all``) is only used by
the test suite; outside tests the app checks at startup that the database is at the latest
revision and reports "not ready" otherwise.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import DateTime, Dialect, Engine, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import TypeDecorator

ROOT = Path(__file__).resolve().parent.parent


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetimes on every backend.

    SQLite has no timezone support and hands back naive values. This type refuses naive input,
    stores UTC, and always returns aware UTC datetimes, so comparisons behave the same on
    SQLite and Postgres.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime; use an aware UTC datetime")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class Base(DeclarativeBase):
    pass


def make_engine(database_url: str) -> Engine:
    if database_url.startswith("sqlite"):
        # timeout: wait for a competing writer (worker + API on one SQLite file) instead of
        # failing immediately with "database is locked".
        kwargs: dict[str, Any] = {"connect_args": {"check_same_thread": False, "timeout": 30}}
        if database_url in ("sqlite://", "sqlite:///:memory:"):
            kwargs["poolclass"] = StaticPool  # one shared in-memory DB across threads
        return create_engine(database_url, **kwargs)
    return create_engine(database_url, pool_pre_ping=True)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def init_db(engine: Engine) -> None:
    """Create all tables directly from the models. Tests only; real runs use Alembic."""
    from app import models  # noqa: F401  (register tables)

    Base.metadata.create_all(engine)


def alembic_head() -> str | None:
    """The newest revision in ``migrations/versions`` (None if it cannot be determined)."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config()
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    return ScriptDirectory.from_config(cfg).get_current_head()


def current_revision(engine: Engine) -> str | None:
    from alembic.runtime.migration import MigrationContext

    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def schema_is_current(engine: Engine) -> bool:
    try:
        return current_revision(engine) == alembic_head()
    except Exception:
        return False


def db_ok(engine: Engine) -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        return False
    return True


def utc(value: datetime) -> datetime:
    """Treat a naive datetime as UTC; convert an aware one to UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
