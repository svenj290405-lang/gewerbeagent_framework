"""Add rechnungen table for Lexware-Rechnungs-Pipeline

Revision ID: e7a3c1f5d8b2
Revises: d4f8e2a1b9c3
Create Date: 2026-05-02 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e7a3c1f5d8b2'
down_revision: Union[str, Sequence[str], None] = 'd4f8e2a1b9c3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Rechnungen: Telegram-Wizard -> Gemini-Extraction -> Lexware-Draft."""
    op.create_table(
        'rechnungen',
        sa.Column('id', sa.dialects.postgresql.UUID(as_uuid=True),
                  server_default=sa.text('gen_random_uuid()'),
                  primary_key=True, nullable=False),
        sa.Column('tenant_id', sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('tenants.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('chat_id', sa.BigInteger(), nullable=True),
        sa.Column('input_type', sa.String(length=10), nullable=False),
        sa.Column('raw_input_text', sa.Text(), nullable=True),
        sa.Column('transcript', sa.Text(), nullable=True),
        sa.Column('extracted_data', sa.dialects.postgresql.JSONB(),
                  nullable=True),
        sa.Column('kunde_name', sa.String(length=255), nullable=True),
        sa.Column('kunde_ort', sa.String(length=255), nullable=True),
        sa.Column('kunde_strasse', sa.String(length=255), nullable=True),
        sa.Column('kunde_plz', sa.String(length=20), nullable=True),
        sa.Column('kunde_email', sa.String(length=255), nullable=True),
        sa.Column('leistung_titel', sa.String(length=500), nullable=True),
        sa.Column('leistung_beschreibung', sa.Text(), nullable=True),
        sa.Column('betrag_brutto_eur', sa.Numeric(precision=10, scale=2),
                  nullable=True),
        sa.Column('lexware_contact_id',
                  sa.dialects.postgresql.UUID(as_uuid=True),
                  nullable=True),
        sa.Column('lexware_invoice_id',
                  sa.dialects.postgresql.UUID(as_uuid=True),
                  nullable=True),
        sa.Column('lexware_voucher_number', sa.String(length=50),
                  nullable=True),
        sa.Column('status', sa.String(length=30), nullable=False,
                  server_default='extracting'),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('mail_sent_to', sa.String(length=255), nullable=True),
        sa.Column('mail_sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.Column('drafted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'),
                  nullable=False),
    )
    op.create_index('ix_rechnungen_tenant_chat', 'rechnungen',
                    ['tenant_id', 'chat_id'], unique=False)
    op.create_index('ix_rechnungen_status', 'rechnungen',
                    ['status'], unique=False)
    op.create_index('ix_rechnungen_lexware_invoice', 'rechnungen',
                    ['lexware_invoice_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_rechnungen_lexware_invoice', table_name='rechnungen')
    op.drop_index('ix_rechnungen_status', table_name='rechnungen')
    op.drop_index('ix_rechnungen_tenant_chat', table_name='rechnungen')
    op.drop_table('rechnungen')
