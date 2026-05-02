"""Add branche + voice_phone_number to tenants

Revision ID: 42c36e7a5370
Revises: c8790780fd31
Create Date: 2026-04-30 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '42c36e7a5370'
down_revision: Union[str, Sequence[str], None] = 'c8790780fd31'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Branche fuer Agent-Mapping (tischler, sanitaer, elektrik, ...)
    op.add_column(
        'tenants',
        sa.Column('branche', sa.String(length=50), nullable=True),
    )
    op.create_index(
        'ix_tenants_branche',
        'tenants',
        ['branche'],
        unique=False,
    )

    # Voice-Phone-Number fuer Tenant-Routing bei eingehenden Calls
    # (E.164 Format z.B. +492187973998912)
    op.add_column(
        'tenants',
        sa.Column('voice_phone_number', sa.String(length=30), nullable=True),
    )
    op.create_index(
        'ix_tenants_voice_phone_number',
        'tenants',
        ['voice_phone_number'],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index('ix_tenants_voice_phone_number', table_name='tenants')
    op.drop_column('tenants', 'voice_phone_number')
    op.drop_index('ix_tenants_branche', table_name='tenants')
    op.drop_column('tenants', 'branche')
