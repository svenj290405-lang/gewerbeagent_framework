"""Add last_q_reply + last_user_message to email_conversations

Revision ID: dcfe33e1d8c6
Revises: 42c36e7a5370
Create Date: 2026-04-30 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'dcfe33e1d8c6'
down_revision: Union[str, Sequence[str], None] = '42c36e7a5370'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Konversations-Memory: was Q zuletzt geantwortet hat + was der Kunde zuletzt schrieb."""
    op.add_column(
        'email_conversations',
        sa.Column('last_q_reply', sa.Text(), nullable=True),
    )
    op.add_column(
        'email_conversations',
        sa.Column('last_user_message', sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('email_conversations', 'last_user_message')
    op.drop_column('email_conversations', 'last_q_reply')
