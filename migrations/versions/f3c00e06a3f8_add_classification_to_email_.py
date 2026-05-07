"""add_classification_to_email_conversations

Revision ID: f3c00e06a3f8
Revises: 0131127e9dc8
Create Date: 2026-05-07

Fuegt 4 Spalten zu email_conversations fuer Subject-Klassifikation:
- classification: RELEVANT_KUNDE / RELEVANT_GESCHAEFT / NICHT_RELEVANT / PRIVAT / UNSICHER
- classification_confidence: low / medium / high
- classification_reason: Gemini's Begruendung
- classified_at: Wann klassifiziert
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f3c00e06a3f8'
down_revision = 'da4048905f90'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'email_conversations',
        sa.Column('classification', sa.String(30), nullable=True),
    )
    op.add_column(
        'email_conversations',
        sa.Column('classification_confidence', sa.String(10), nullable=True),
    )
    op.add_column(
        'email_conversations',
        sa.Column('classification_reason', sa.Text(), nullable=True),
    )
    op.add_column(
        'email_conversations',
        sa.Column('classified_at', sa.DateTime(timezone=True), nullable=True),
    )

    # Index fuer Dashboard-Queries (Kategorie pro Tag)
    op.create_index(
        'ix_email_conv_classification',
        'email_conversations',
        ['tenant_id', 'classification', 'classified_at'],
    )


def downgrade() -> None:
    op.drop_index('ix_email_conv_classification', table_name='email_conversations')
    op.drop_column('email_conversations', 'classified_at')
    op.drop_column('email_conversations', 'classification_reason')
    op.drop_column('email_conversations', 'classification_confidence')
    op.drop_column('email_conversations', 'classification')
