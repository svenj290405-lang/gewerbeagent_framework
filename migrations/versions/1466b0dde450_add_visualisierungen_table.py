"""Add visualisierungen table for AI-generated room renderings

Revision ID: 1466b0dde450
Revises: dcfe33e1d8c6
Create Date: 2026-04-30 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '1466b0dde450'
down_revision: Union[str, Sequence[str], None] = 'dcfe33e1d8c6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Visualisierungen: Foto + Prompt -> KI-Bild fuer Kunden-Demo."""
    op.create_table(
        'visualisierungen',
        sa.Column('id', sa.dialects.postgresql.UUID(as_uuid=True),
                  server_default=sa.text('gen_random_uuid()'),
                  primary_key=True, nullable=False),
        sa.Column('tenant_id', sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('tenants.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('chat_id', sa.BigInteger(), nullable=True),
        sa.Column('kunde_email', sa.String(length=255), nullable=True),
        sa.Column('kunde_name', sa.String(length=255), nullable=True),
        sa.Column('original_image_data', sa.LargeBinary(), nullable=True),
        sa.Column('result_image_data', sa.LargeBinary(), nullable=True),
        sa.Column('prompt', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=30), nullable=False,
                  server_default='pending'),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column(
            'completed_at',
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_index(
        'ix_visualisierungen_tenant_chat',
        'visualisierungen',
        ['tenant_id', 'chat_id'],
        unique=False,
    )
    op.create_index(
        'ix_visualisierungen_status',
        'visualisierungen',
        ['status'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_visualisierungen_status', table_name='visualisierungen')
    op.drop_index('ix_visualisierungen_tenant_chat', table_name='visualisierungen')
    op.drop_table('visualisierungen')
