"""two-factor authentication (TOTP) and recovery codes

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("totp_secret_enc", sa.Text(), nullable=True))
        batch.add_column(sa.Column("totp_pending_enc", sa.Text(), nullable=True))
        batch.add_column(sa.Column("totp_enabled_at", TS, nullable=True))
        batch.add_column(sa.Column("totp_last_step", sa.Integer(), nullable=True))
    op.create_table(
        "recovery_codes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("code_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("used_at", TS, nullable=True),
    )
    op.create_index("ix_recovery_codes_user_id", "recovery_codes", ["user_id"])


def downgrade() -> None:
    op.drop_table("recovery_codes")
    with op.batch_alter_table("users") as batch:
        for col in ("totp_last_step", "totp_enabled_at", "totp_pending_enc", "totp_secret_enc"):
            batch.drop_column(col)
