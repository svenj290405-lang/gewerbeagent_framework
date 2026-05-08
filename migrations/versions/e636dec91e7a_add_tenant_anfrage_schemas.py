"""add_tenant_anfrage_schemas

Revision ID: e636dec91e7a
Revises: 2decee0aa2ec
Create Date: 2026-05-08

Per-Tenant-Customizable Anfrage-Formular-Schemas.

Wenn ein Tenant ein eigenes Schema fuer einen Anfrage-Typ pflegt
(z.B. tischler), wird das aus DB geladen statt aus Hardcode.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = 'e636dec91e7a'
down_revision = '2decee0aa2ec'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'tenant_anfrage_schemas',
        sa.Column('id', UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column('tenant_id', UUID(as_uuid=True), sa.ForeignKey('tenants.id', ondelete='CASCADE'), nullable=False),
        sa.Column('anfrage_typ', sa.String(50), nullable=False),
        sa.Column('title', sa.String(200), nullable=True),
        sa.Column('subtitle', sa.String(500), nullable=True),
        sa.Column('fields', JSONB, nullable=False),
        sa.Column('is_active', sa.Boolean, nullable=False, server_default=sa.text('true')),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        'ux_tenant_anfrage_schema',
        'tenant_anfrage_schemas',
        ['tenant_id', 'anfrage_typ'],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index('ux_tenant_anfrage_schema', table_name='tenant_anfrage_schemas')
    op.drop_table('tenant_anfrage_schemas')
