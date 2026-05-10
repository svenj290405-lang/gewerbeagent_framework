"""tenant_kalkulationen: mathematische Formeln pro Tenant

Revision ID: r4m8h3k6n2p7
Revises: q3l7h2j5g9k4
Create Date: 2026-05-10 23:30:00.000000

Schwester-Tabelle zu tenant_knowledge: speichert Berechnungs-Formeln,
die der Handwerker via Telegram (/kalkulation) oder Excel-Upload pflegt.
Wird bei der Angebots-Extraktion in den Gemini-Prompt eingespeist;
Variable werden von Gemini gefuellt, der finale Preis wird deterministisch
in Python (simpleeval) gerechnet (Hybrid-Modus).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'r4m8h3k6n2p7'
down_revision: Union[str, Sequence[str], None] = 'q3l7h2j5g9k4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'tenant_kalkulationen',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('tenant_id', sa.UUID(), nullable=False),
        sa.Column('kategorie', sa.String(length=50), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('formel', sa.String(length=1000), nullable=False),
        sa.Column(
            'variablen',
            sa.ARRAY(sa.String()),
            nullable=False,
            server_default='{}',
        ),
        sa.Column('einheit', sa.String(length=50), nullable=True),
        sa.Column('beschreibung', sa.Text(), nullable=True),
        sa.Column(
            'aktiv', sa.Boolean(), nullable=False, server_default=sa.text('true')
        ),
        sa.Column(
            'sortierung', sa.Integer(), nullable=False, server_default='0'
        ),
        sa.Column(
            'source',
            sa.String(length=20),
            nullable=False,
            server_default='manual',
        ),
        sa.Column('excel_filename', sa.String(length=255), nullable=True),
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
        'ix_tenant_kalkulationen_tenant_id',
        'tenant_kalkulationen',
        ['tenant_id'],
        unique=False,
    )
    op.create_index(
        'ix_tenant_kalkulationen_kategorie',
        'tenant_kalkulationen',
        ['kategorie'],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        'ix_tenant_kalkulationen_kategorie', table_name='tenant_kalkulationen'
    )
    op.drop_index(
        'ix_tenant_kalkulationen_tenant_id', table_name='tenant_kalkulationen'
    )
    op.drop_table('tenant_kalkulationen')
