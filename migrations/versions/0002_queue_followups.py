"""pipeline, job queue, follow-ups, handoff tasks, kill switch, CRM sync (session 3)

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def _lead_columns() -> list[sa.Column[object]]:
    return [
        sa.Column("timezone", sa.String(64), nullable=True),
        sa.Column("lost_reason", sa.String(200), nullable=True),
        sa.Column("sequence_state", sa.String(16), nullable=False, server_default="active"),
        sa.Column("opted_out_at", TS, nullable=True),
        sa.Column("crm_contact_id", sa.String(64), nullable=True),
        sa.Column("crm_deal_id", sa.String(64), nullable=True),
        sa.Column("crm_synced_at", TS, nullable=True),
    ]


def _lead_fk(ondelete: str = "CASCADE", nullable: bool = False) -> sa.Column[object]:
    return sa.Column(
        "lead_id", sa.Uuid(), sa.ForeignKey("leads.id", ondelete=ondelete), nullable=nullable
    )


def upgrade() -> None:
    with op.batch_alter_table("leads") as batch:
        for col in _lead_columns():
            batch.add_column(col)
    # The ORM always sets sequence_state; the server default only backfilled old rows.
    with op.batch_alter_table("leads") as batch:
        batch.alter_column("sequence_state", server_default=None)

    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("run_at", TS, nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("locked_by", sa.String(64), nullable=True),
        sa.Column("locked_until", TS, nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("last_error", sa.String(500), nullable=True),
        _lead_fk(nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
    )
    op.create_index("ix_jobs_kind", "jobs", ["kind"])
    op.create_index("ix_jobs_lead_id", "jobs", ["lead_id"])
    op.create_index("ix_jobs_claim", "jobs", ["status", "run_at"])

    op.create_table(
        "outbox_messages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        _lead_fk(),
        sa.Column("step", sa.String(32), nullable=False),
        sa.Column("channel", sa.String(8), nullable=False),
        sa.Column("to_address", sa.String(254), nullable=False),
        sa.Column("subject", sa.String(200), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("sender", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("transactional", sa.Boolean(), nullable=False),
        sa.Column("sent_at", TS, nullable=False),
    )
    op.create_index("ix_outbox_messages_lead_id", "outbox_messages", ["lead_id"])
    op.create_index("ix_outbox_messages_sent_at", "outbox_messages", ["sent_at"])

    op.create_table(
        "tasks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        _lead_fk(),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("detail", sa.String(300), nullable=True),
        sa.Column("assignee", sa.String(64), nullable=True),
        sa.Column("status", sa.String(8), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("done_at", TS, nullable=True),
    )
    op.create_index("ix_tasks_lead_id", "tasks", ["lead_id"])
    op.create_index("ix_tasks_status", "tasks", ["status"])

    op.create_table(
        "system_flags",
        sa.Column("key", sa.String(32), primary_key=True),
        sa.Column("value", sa.String(64), nullable=False),
        sa.Column("updated_by", sa.String(48), nullable=False),
        sa.Column("updated_at", TS, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("system_flags")
    op.drop_table("tasks")
    op.drop_table("outbox_messages")
    op.drop_table("jobs")
    with op.batch_alter_table("leads") as batch:
        for col in reversed(_lead_columns()):
            batch.drop_column(col.name)
