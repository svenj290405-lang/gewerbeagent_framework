"""Add telegram_state table for conversation state-machine

Revision ID: fe07cd7a9acc
Revises: 850b5444d9f1
Create Date: 2026-04-30 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'fe07cd7a9acc'
down_revision: Union[str, Sequence[str], None] = '850b5444d9f1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'telegram_state',
        sa.Column('chat_id', sa.BigInteger(), nullable=False),
        sa.Column('state_key', sa.String(length=50), nullable=False),
        sa.Column('state_data', sa.JSON(), nullable=True),
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
        sa.Column(
            'expires_at',
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint('chat_id'),
    )
    op.create_index(
        'ix_telegram_state_expires_at',
        'telegram_state',
        ['expires_at'],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_telegram_state_expires_at', table_name='telegram_state')
    op.drop_table('telegram_state')
