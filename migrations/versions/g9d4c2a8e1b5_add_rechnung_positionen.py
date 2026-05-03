"""Add rechnung_positionen table for multi-line invoices

Revision ID: g9d4c2a8e1b5
Revises: e7a3c1f5d8b2
Create Date: 2026-05-02 19:35:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'g9d4c2a8e1b5'
down_revision: Union[str, Sequence[str], None] = 'e7a3c1f5d8b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Mehrere Positionen pro Rechnung."""
    op.create_table(
        'rechnung_positionen',
        sa.Column('id', sa.dialects.postgresql.UUID(as_uuid=True),
                  server_default=sa.text('gen_random_uuid()'),
                  primary_key=True, nullable=False),
        sa.Column('rechnung_id', sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('rechnungen.id', ondelete='CASCADE'),
                  nullable=False),

        # Position-Reihenfolge (1, 2, 3, ...)
        sa.Column('position_nr', sa.Integer(), nullable=False),

        # Inhalt
        sa.Column('name', sa.String(length=500), nullable=False),
        sa.Column('beschreibung', sa.Text(), nullable=True),
        sa.Column('menge', sa.Numeric(precision=12, scale=3),
                  nullable=False, server_default='1'),
        sa.Column('einheit', sa.String(length=50),
                  nullable=False, server_default='Stueck'),
        sa.Column('preis_brutto_eur',
                  sa.Numeric(precision=10, scale=2),
                  nullable=False),
        sa.Column('mwst_prozent', sa.Integer(),
                  nullable=False, server_default='19'),

        # Timestamp
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
    )

    op.create_index('ix_rechnung_positionen_rechnung',
                    'rechnung_positionen',
                    ['rechnung_id', 'position_nr'],
                    unique=True)
    op.create_index('ix_rechnung_positionen_name',
                    'rechnung_positionen',
                    ['name'],
                    unique=False)


def downgrade() -> None:
    op.drop_index('ix_rechnung_positionen_name',
                  table_name='rechnung_positionen')
    op.drop_index('ix_rechnung_positionen_rechnung',
                  table_name='rechnung_positionen')
    op.drop_table('rechnung_positionen')
