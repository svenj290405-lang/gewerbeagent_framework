"""Add updated_at to visualisierungen

Revision ID: c86539f95455
Revises: 1466b0dde450
Create Date: 2026-04-30 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c86539f95455'
down_revision: Union[str, Sequence[str], None] = '1466b0dde450'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'visualisierungen',
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column('visualisierungen', 'updated_at')
