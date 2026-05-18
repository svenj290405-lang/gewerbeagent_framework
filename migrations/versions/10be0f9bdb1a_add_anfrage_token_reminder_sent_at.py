"""anfrage_tokens.reminder_sent_at fuer 24h-Erinnerungs-Cron

Wird vom Reminder-Cron gesetzt sobald die Erinnerungs-Mail an den
Kunden raus ist. Verhindert dass derselbe Token mehrere Mails kriegt
wenn der Cron-Lauf wiederholt das gleiche Fenster trifft.

Revision ID: 10be0f9bdb1a
Revises: aa6bd7ce8df9
Create Date: 2026-05-18 13:49:52.929812

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '10be0f9bdb1a'
down_revision: Union[str, Sequence[str], None] = 'aa6bd7ce8df9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "anfrage_tokens",
        sa.Column(
            "reminder_sent_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("anfrage_tokens", "reminder_sent_at")
