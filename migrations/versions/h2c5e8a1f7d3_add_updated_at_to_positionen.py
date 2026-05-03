"""Add updated_at to rechnung_positionen (Base auto-injects it)

Revision ID: h2c5e8a1f7d3
Revises: g9d4c2a8e1b5
Create Date: 2026-05-02 19:45:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'h2c5e8a1f7d3'
down_revision: Union[str, Sequence[str], None] = 'g9d4c2a8e1b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Base.updated_at war nicht in der initialen Migration."""
    op.add_column(
        'rechnung_positionen',
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column('rechnung_positionen', 'updated_at')
