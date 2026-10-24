"""initial schema: leads, activities, webhook_events (sessions 1-2)

Revision ID: 0001
Revises:
Create Date: 2026-10-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "leads",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("email", sa.String(254), nullable=True),
        sa.Column("phone", sa.String(16), nullable=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("stage", sa.String(16), nullable=False),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("score_summary", sa.String(300), nullable=True),
        sa.Column("next_step", sa.String(16), nullable=True),
        sa.Column("score_confidence", sa.Float(), nullable=True),
        sa.Column("score_flags", sa.JSON(), nullable=True),
        sa.Column("scored_by", sa.String(16), nullable=True),
        sa.Column("score_provider", sa.String(16), nullable=True),
        sa.Column("score_latency_ms", sa.Integer(), nullable=True),
        sa.Column("scored_at", TS, nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("first_response_at", TS, nullable=True),
    )
    for col in ("email", "phone", "source", "stage", "created_at"):
        op.create_index(f"ix_leads_{col}", "leads", [col])

    op.create_table(
        "activities",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "lead_id", sa.Uuid(), sa.ForeignKey("leads.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("actor", sa.String(32), nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_index("ix_activities_lead_id", "activities", ["lead_id"])
    op.create_index("ix_activities_kind", "activities", ["kind"])

    op.create_table(
        "webhook_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("event_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "lead_id", sa.Uuid(), sa.ForeignKey("leads.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("reason", sa.String(200), nullable=True),
        sa.Column("received_at", TS, nullable=False),
        sa.UniqueConstraint("provider", "event_id", name="uq_webhook_provider_event"),
    )


def downgrade() -> None:
    op.drop_table("webhook_events")
    op.drop_table("activities")
    op.drop_table("leads")
