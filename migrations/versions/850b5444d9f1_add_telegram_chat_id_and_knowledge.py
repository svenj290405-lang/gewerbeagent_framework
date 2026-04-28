"""Add telegram_chat_id to tenants + tenant_knowledge table

Revision ID: 850b5444d9f1
Revises: fe063beab4c7
Create Date: 2026-04-28 22:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '850b5444d9f1'
down_revision: Union[str, Sequence[str], None] = 'fe063beab4c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 1. tenants.telegram_chat_id (BIGINT, nullable, indexed)
    op.add_column(
        'tenants',
        sa.Column('telegram_chat_id', sa.BigInteger(), nullable=True),
    )
    op.create_index(
        'ix_tenants_telegram_chat_id',
        'tenants',
        ['telegram_chat_id'],
        unique=False,
    )

    # 2. tenant_knowledge: Wissensbasis pro Tenant, kategorisiert
    op.create_table(
        'tenant_knowledge',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('tenant_id', sa.UUID(), nullable=False),
        sa.Column('kategorie', sa.String(length=50), nullable=False),
        sa.Column('text', sa.String(length=2000), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ['tenant_id'], ['tenants.id'], ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_tenant_knowledge_tenant_id',
        'tenant_knowledge',
        ['tenant_id'],
        unique=False,
    )
    op.create_index(
        'ix_tenant_knowledge_kategorie',
        'tenant_knowledge',
        ['kategorie'],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        'ix_tenant_knowledge_kategorie', table_name='tenant_knowledge'
    )
    op.drop_index(
        'ix_tenant_knowledge_tenant_id', table_name='tenant_knowledge'
    )
    op.drop_table('tenant_knowledge')

    op.drop_index('ix_tenants_telegram_chat_id', table_name='tenants')
    op.drop_column('tenants', 'telegram_chat_id')
