"""Add angebote.arbeit_fortschritt (0-100 %)

Revision ID: e1a4c7f2b9d3
Revises: d7f3a1c9e6b4
Create Date: 2026-06-05 10:00:00.000000

Fortschritts-Regler im "Aktuelles"-Tab der PWA: laufende Auftraege
(Status arbeit_laeuft) bekommen einen 0-100 %-Regler, den der Handwerker
zieht. Bei 100 % wird der Auftrag fertiggemeldet und in Q zur Rechnung
gefuehrt. Der Wert muss persistieren (Geraetewechsel/Neustart).

Additiv. Bestehende Angebote bekommen 0.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e1a4c7f2b9d3"
down_revision: Union[str, None] = "d7f3a1c9e6b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "angebote",
        sa.Column(
            "arbeit_fortschritt",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("angebote", "arbeit_fortschritt")
