"""encrypt lead phone/email and outbox addresses at rest, with blind-index columns

Adds ``*_enc`` (Fernet ciphertext) and ``*_bidx`` (HMAC blind index) columns, backfills
them from the plaintext columns with the keys in FIELD_ENCRYPTION_KEY / BLIND_INDEX_KEY,
then drops the plaintext columns. The downgrade decrypts back.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.security import fields

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

leads = sa.table(
    "leads",
    sa.column("id", sa.Uuid()),
    sa.column("phone", sa.String()),
    sa.column("email", sa.String()),
    sa.column("phone_enc", sa.Text()),
    sa.column("phone_bidx", sa.String()),
    sa.column("email_enc", sa.Text()),
    sa.column("email_bidx", sa.String()),
)
outbox = sa.table(
    "outbox_messages",
    sa.column("id", sa.Integer()),
    sa.column("to_address", sa.String()),
    sa.column("to_address_enc", sa.Text()),
)


def upgrade() -> None:
    crypto = fields.active()
    with op.batch_alter_table("leads") as batch:
        batch.add_column(sa.Column("email_enc", sa.Text(), nullable=True))
        batch.add_column(sa.Column("email_bidx", sa.String(64), nullable=True))
        batch.add_column(sa.Column("phone_enc", sa.Text(), nullable=True))
        batch.add_column(sa.Column("phone_bidx", sa.String(64), nullable=True))
    with op.batch_alter_table("outbox_messages") as batch:
        batch.add_column(sa.Column("to_address_enc", sa.Text(), nullable=True))

    conn = op.get_bind()
    for lead_id, phone, email in conn.execute(
        sa.select(leads.c.id, leads.c.phone, leads.c.email)
    ).all():
        conn.execute(
            leads.update()
            .where(leads.c.id == lead_id)
            .values(
                phone_enc=crypto.encrypt(phone) if phone else None,
                phone_bidx=crypto.blind_index("phone", phone) if phone else None,
                email_enc=crypto.encrypt(email) if email else None,
                email_bidx=crypto.blind_index("email", email) if email else None,
            )
        )
    for msg_id, to_address in conn.execute(sa.select(outbox.c.id, outbox.c.to_address)).all():
        conn.execute(
            outbox.update()
            .where(outbox.c.id == msg_id)
            .values(to_address_enc=crypto.encrypt(to_address))
        )

    with op.batch_alter_table("leads") as batch:
        batch.drop_index("ix_leads_email")
        batch.drop_index("ix_leads_phone")
        batch.drop_column("email")
        batch.drop_column("phone")
        batch.create_index("ix_leads_email_bidx", ["email_bidx"])
        batch.create_index("ix_leads_phone_bidx", ["phone_bidx"])
    with op.batch_alter_table("outbox_messages") as batch:
        batch.drop_column("to_address")
        batch.alter_column("to_address_enc", existing_type=sa.Text(), nullable=False)


def downgrade() -> None:
    crypto = fields.active()
    with op.batch_alter_table("leads") as batch:
        batch.add_column(sa.Column("email", sa.String(254), nullable=True))
        batch.add_column(sa.Column("phone", sa.String(16), nullable=True))
    with op.batch_alter_table("outbox_messages") as batch:
        batch.add_column(sa.Column("to_address", sa.String(254), nullable=True))

    conn = op.get_bind()
    for lead_id, phone_enc, email_enc in conn.execute(
        sa.select(leads.c.id, leads.c.phone_enc, leads.c.email_enc)
    ).all():
        conn.execute(
            leads.update()
            .where(leads.c.id == lead_id)
            .values(
                phone=crypto.decrypt(phone_enc) if phone_enc else None,
                email=crypto.decrypt(email_enc) if email_enc else None,
            )
        )
    for msg_id, enc in conn.execute(sa.select(outbox.c.id, outbox.c.to_address_enc)).all():
        conn.execute(
            outbox.update().where(outbox.c.id == msg_id).values(to_address=crypto.decrypt(enc))
        )

    with op.batch_alter_table("outbox_messages") as batch:
        batch.drop_column("to_address_enc")
        batch.alter_column("to_address", existing_type=sa.String(254), nullable=False)
    with op.batch_alter_table("leads") as batch:
        batch.drop_index("ix_leads_email_bidx")
        batch.drop_index("ix_leads_phone_bidx")
        for col in ("email_enc", "email_bidx", "phone_enc", "phone_bidx"):
            batch.drop_column(col)
        batch.create_index("ix_leads_email", ["email"])
        batch.create_index("ix_leads_phone", ["phone"])
