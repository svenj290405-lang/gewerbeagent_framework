"""Add belege table for Lexware-Beleg-Pipeline

Revision ID: d4f8e2a1b9c3
Revises: c86539f95455
Create Date: 2026-05-02 08:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd4f8e2a1b9c3'
down_revision: Union[str, Sequence[str], None] = 'c86539f95455'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Belege: Foto/PDF -> Lexware-Upload mit Audit-Trail."""
    op.create_table(
        'belege',
        sa.Column('id', sa.dialects.postgresql.UUID(as_uuid=True),
                  server_default=sa.text('gen_random_uuid()'),
                  primary_key=True, nullable=False),
        sa.Column('tenant_id', sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('tenants.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('chat_id', sa.BigInteger(), nullable=True),
        sa.Column('file_data', sa.LargeBinary(), nullable=False),
        sa.Column('file_mime', sa.String(length=100), nullable=False),
        sa.Column('file_hash', sa.String(length=64), nullable=False),
        sa.Column('file_size', sa.Integer(), nullable=True),
        sa.Column('original_filename', sa.String(length=255), nullable=True),
        sa.Column('source', sa.String(length=20), nullable=False,
                  server_default='telegram'),
        sa.Column('caption', sa.Text(), nullable=True),
        sa.Column('lexware_file_id',
                  sa.dialects.postgresql.UUID(as_uuid=True),
                  nullable=True),
        sa.Column('lexware_voucher_id',
                  sa.dialects.postgresql.UUID(as_uuid=True),
                  nullable=True),
        sa.Column('status', sa.String(length=30), nullable=False,
                  server_default='pending'),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('upload_attempts', sa.Integer(), nullable=False,
                  server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.Column('uploaded_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'),
                  nullable=False),
    )
    op.create_index('ix_belege_tenant_chat', 'belege',
                    ['tenant_id', 'chat_id'], unique=False)
    op.create_index('ix_belege_status', 'belege',
                    ['status'], unique=False)
    op.create_index('ix_belege_tenant_hash', 'belege',
                    ['tenant_id', 'file_hash'], unique=True)


def downgrade() -> None:
    op.drop_index('ix_belege_tenant_hash', table_name='belege')
    op.drop_index('ix_belege_status', table_name='belege')
    op.drop_index('ix_belege_tenant_chat', table_name='belege')
    op.drop_table('belege')
