"""users and roles, server-side sessions, audit log, lead assignee, retention marker

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("email", sa.String(254), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("failed_logins", sa.Integer(), nullable=False),
        sa.Column("locked_until", TS, nullable=True),
        sa.Column("last_login_at", TS, nullable=True),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_table(
        "user_sessions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("last_seen_at", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("revoked_at", TS, nullable=True),
        sa.Column("ip_hash", sa.String(32), nullable=True),
        sa.Column("user_agent", sa.String(200), nullable=True),
    )
    op.create_index("ix_user_sessions_user_id", "user_sessions", ["user_id"])
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("at", TS, nullable=False),
        sa.Column("actor", sa.String(254), nullable=False),
        sa.Column("role", sa.String(16), nullable=True),
        sa.Column("action", sa.String(48), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=True),
        sa.Column("target_id", sa.String(64), nullable=True),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("ip_hash", sa.String(32), nullable=True),
        sa.Column("user_agent", sa.String(200), nullable=True),
    )
    for col in ("at", "action", "target_id"):
        op.create_index(f"ix_audit_events_{col}", "audit_events", [col])
    with op.batch_alter_table("leads") as batch:
        batch.add_column(sa.Column("assignee_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("anonymized_at", TS, nullable=True))
        batch.create_foreign_key(
            "fk_leads_assignee_id_users", "users", ["assignee_id"], ["id"], ondelete="SET NULL"
        )
        batch.create_index("ix_leads_assignee_id", ["assignee_id"])


def downgrade() -> None:
    with op.batch_alter_table("leads") as batch:
        batch.drop_index("ix_leads_assignee_id")
        batch.drop_constraint("fk_leads_assignee_id_users", type_="foreignkey")
        batch.drop_column("anonymized_at")
        batch.drop_column("assignee_id")
    op.drop_table("audit_events")
    op.drop_table("user_sessions")
    op.drop_table("users")
