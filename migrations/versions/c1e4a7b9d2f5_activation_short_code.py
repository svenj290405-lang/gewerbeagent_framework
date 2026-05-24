"""employee_activation_tokens: kurzer short_code fuer Suche-Onboarding

Onboarding per Telegram-Suche (statt Deep-Link): der Kunde sucht den Bot,
drueckt START und tippt einen kurzen Code (8 Zeichen). Dafuer bekommt der
Aktivierungs-Token zusaetzlich einen `short_code`. Der lange `token`
(Deep-Link) bleibt unveraendert.

Additiv: ADD COLUMN short_code (nullable — Bestands-Zeilen bleiben NULL)
+ unique Index (Postgres laesst mehrere NULLs zu).

Revision ID: c1e4a7b9d2f5
Revises: b8d2f4a6c1e3
Create Date: 2026-05-24 20:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c1e4a7b9d2f5"
down_revision: Union[str, Sequence[str], None] = "b8d2f4a6c1e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "employee_activation_tokens",
        sa.Column("short_code", sa.String(length=16), nullable=True),
    )
    op.create_index(
        "ix_activation_short_code",
        "employee_activation_tokens",
        ["short_code"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_activation_short_code", table_name="employee_activation_tokens",
    )
    op.drop_column("employee_activation_tokens", "short_code")
