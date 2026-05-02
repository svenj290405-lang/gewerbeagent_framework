"""Remove auto-generated default from telegram_state.chat_id

Revision ID: c8790780fd31
Revises: fe07cd7a9acc
Create Date: 2026-04-30 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c8790780fd31'
down_revision: Union[str, Sequence[str], None] = 'fe07cd7a9acc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Remove the auto-generated sequence default from chat_id."""
    op.execute("ALTER TABLE telegram_state ALTER COLUMN chat_id DROP DEFAULT")
    op.execute("DROP SEQUENCE IF EXISTS telegram_state_chat_id_seq CASCADE")


def downgrade() -> None:
    """Re-add the sequence (best-effort, mostly cosmetic)."""
    op.execute("CREATE SEQUENCE IF NOT EXISTS telegram_state_chat_id_seq AS BIGINT")
    op.execute("ALTER TABLE telegram_state ALTER COLUMN chat_id SET DEFAULT nextval('telegram_state_chat_id_seq')")
