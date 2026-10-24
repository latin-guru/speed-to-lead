"""interview mode: single-use invite links

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "interview_invites",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("label", sa.String(80), nullable=True),
        sa.Column("created_by", sa.String(254), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("revoked_at", TS, nullable=True),
        sa.Column("used_at", TS, nullable=True),
        sa.Column("viewer_hash", sa.String(64), nullable=True),
        sa.Column(
            "lead_id", sa.Uuid(), sa.ForeignKey("leads.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("email_result", sa.String(32), nullable=True),
        sa.Column("email_sent_at", TS, nullable=True),
    )
    op.create_index("ix_interview_invites_expires_at", "interview_invites", ["expires_at"])
    op.create_index("ix_interview_invites_lead_id", "interview_invites", ["lead_id"])


def downgrade() -> None:
    op.drop_table("interview_invites")
