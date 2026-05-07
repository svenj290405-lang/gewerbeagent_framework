"""add_anfrage_forms

Revision ID: 2decee0aa2ec
Revises: f3c00e06a3f8
Create Date: 2026-05-07

Anfrage-Formulare per Token-Link:
- anfrage_tokens: Token + Tenant + Kunde + Anfrage-Typ + Ablaufdatum
- anfrage_responses: Antworten als JSONB nach Submit
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = '2decee0aa2ec'
down_revision = 'f3c00e06a3f8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'anfrage_tokens',
        sa.Column('id', UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column('token', sa.String(64), nullable=False),
        sa.Column('tenant_id', UUID(as_uuid=True), sa.ForeignKey('tenants.id', ondelete='CASCADE'), nullable=False),
        sa.Column('kunde_email', sa.String(255), nullable=False),
        sa.Column('kunde_name', sa.String(255), nullable=True),
        sa.Column('anfrage_typ', sa.String(50), nullable=False, server_default='allgemein'),
        sa.Column('original_subject', sa.String(500), nullable=True),
        sa.Column('original_message_id', sa.String(500), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('ix_anfrage_token', 'anfrage_tokens', ['token'], unique=True)
    op.create_index('ix_anfrage_tenant', 'anfrage_tokens', ['tenant_id', 'created_at'])

    op.create_table(
        'anfrage_responses',
        sa.Column('id', UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column('token_id', UUID(as_uuid=True), sa.ForeignKey('anfrage_tokens.id', ondelete='CASCADE'), nullable=False),
        sa.Column('antworten', JSONB, nullable=False),
        sa.Column('submitted_ip', sa.String(50), nullable=True),
        sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('ix_anfrage_response_token', 'anfrage_responses', ['token_id'])


def downgrade() -> None:
    op.drop_index('ix_anfrage_response_token', table_name='anfrage_responses')
    op.drop_table('anfrage_responses')
    op.drop_index('ix_anfrage_tenant', table_name='anfrage_tokens')
    op.drop_index('ix_anfrage_token', table_name='anfrage_tokens')
    op.drop_table('anfrage_tokens')
