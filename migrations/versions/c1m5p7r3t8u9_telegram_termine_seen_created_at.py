"""telegram_termine_seen.created_at nachziehen

Revision ID: c1m5p7r3t8u9
Revises: b8l4n6p9r2s5
Create Date: 2026-05-17 19:55:00.000000

Hotfix: die Originalmigration b8l4n6p9r2s5 hat nur chat_id/event_ids/
updated_at angelegt, aber Base hat `created_at` als nicht-nullable
Default-Spalte. SQLAlchemy generiert `SELECT ... created_at ...` und
Postgres wirft UndefinedColumnError — /neue_termine crasht beim
Read-Pfad.

Fix: created_at additiv anlegen mit gleichem Default (now()) wie
die Base-Convention.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c1m5p7r3t8u9"
down_revision: Union[str, None] = "b8l4n6p9r2s5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "telegram_termine_seen",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_column("telegram_termine_seen", "created_at")
