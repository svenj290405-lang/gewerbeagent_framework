"""Add employees.app_onboarding_completed_at (PWA-Einrichtungs-Tour)

Revision ID: f3b8d1a2c4e7
Revises: e1a4c7f2b9d3
Create Date: 2026-06-08 10:00:00.000000

Die PWA bekommt eine Ersteinrichtungs-Tour fuer neue Nutzer: Funktions-
Erklaerung (die drei Bereiche + was Q erledigt) und gefuehrtes Verbinden
von Google/Outlook/Lexware. Damit die Tour nur EINMAL und geraeteueber-
greifend erscheint, merkt sie sich der Abschluss am Employee (nicht im
localStorage).

NULL = noch nie gesehen -> Tour startet beim naechsten App-Start. Bestehende
Mitarbeiter kennen die App bereits und werden als "fertig" markiert (NOW()),
damit ihnen die Tour nicht ploetzlich aufgedraengt wird — analog zur
Telegram-Onboarding-Migration. Neue Mitarbeiter starten mit NULL.

Additiv.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f3b8d1a2c4e7"
down_revision: Union[str, None] = "e1a4c7f2b9d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "employees",
        sa.Column(
            "app_onboarding_completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    # Bestandsnutzer als "fertig" markieren — sie sollen die Tour nicht
    # zwangsweise sehen. Nur kuenftig neu angelegte Mitarbeiter bekommen sie.
    op.execute(
        "UPDATE employees SET app_onboarding_completed_at = NOW() "
        "WHERE app_onboarding_completed_at IS NULL"
    )


def downgrade() -> None:
    op.drop_column("employees", "app_onboarding_completed_at")
