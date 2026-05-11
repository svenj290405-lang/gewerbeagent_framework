"""Add failed_mail_queue

Revision ID: v1xy2zk3l4m5
Revises: u9v5w7x4y8z2
Create Date: 2026-05-11 06:00:00.000000

Tabelle fuer Mail-Retry-Queue (Phase A5). Wenn Brevo down ist oder
einen Rechnung-Mail-Versand ablehnt, landet die Mail hier statt sofort
auf 'error' zu gehen. Cron `mail_retry_cron.py` arbeitet die Queue
mit Exponential-Backoff ab.

Additiv. Kein Effekt auf bestehende Tabellen.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "v1xy2zk3l4m5"
down_revision: Union[str, Sequence[str], None] = "u9v5w7x4y8z2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "failed_mail_queue",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Optionaler Backref auf Rechnung — wenn die Mail eine Rechnung
        # mailen sollte. Bei reinen Reply-Mails NULL.
        sa.Column(
            "rechnung_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("rechnungen.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # 'rechnung' / 'visualisierung' / 'reply' / 'angebot' — fuer
        # Caller-Spezifische Recovery-Logik (siehe mail_retry_cron).
        sa.Column("mail_type", sa.String(length=32), nullable=False),
        sa.Column("recipient_email", sa.String(length=320), nullable=False),
        # subject, html_body, attachments-refs als JSONB-Payload.
        # Anhaenge sind als {filename, mime_type, data_base64} eingebettet
        # — Pragmatik: vermeidet separates Blob-Storage fuer max 3-5MB
        # Rechnungs-PDFs. Wenn das nicht reicht, spaeter auf S3/Drive
        # auslagern.
        sa.Column(
            "payload", postgresql.JSONB(), nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "attempt_count", sa.Integer(), nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "next_attempt_at", sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        # 'pending' | 'sent' | 'dead'
        sa.Column(
            "status", sa.String(length=16), nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )
    # Index fuer den Cron-Lookup: alle pending, deren next_attempt_at <= now()
    op.create_index(
        "ix_failed_mail_queue_status_next_attempt",
        "failed_mail_queue",
        ["status", "next_attempt_at"],
    )
    # Index fuer Tenant-Lookup (Admin-UI Phase B)
    op.create_index(
        "ix_failed_mail_queue_tenant_status",
        "failed_mail_queue",
        ["tenant_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_failed_mail_queue_tenant_status",
        table_name="failed_mail_queue",
    )
    op.drop_index(
        "ix_failed_mail_queue_status_next_attempt",
        table_name="failed_mail_queue",
    )
    op.drop_table("failed_mail_queue")
