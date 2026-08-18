"""Scope-Profil im OAuth-State mitfuehren

Revision ID: c3t9r5p2m8q4
Revises: b2s8q4n1l6p7
Create Date: 2026-08-18 14:00:00.000000

Mitarbeiter verbinden ihren Kalender ab jetzt mit einem ENGEREN
Google-Scope als der Inhaber (nur Verfuegbarkeit + ein von uns
angelegter Zweitkalender, kein Zugriff auf private Termine).

Damit gibt es zwei Scope-Listen — und die Liste wird an ZWEI Stellen
gebraucht: beim Bauen der Auth-URL und beim Token-Tausch im Callback.
Zwischen beiden liegt nur der OAuthState. Ohne diese Spalte baut der
Callback den Flow mit der falschen Liste und oauthlib wirft
"Scope has changed".

Nullable: bestehende States (Sekunden bis Minuten alt) und der
Inhaber-Pfad kommen ohne aus — NULL bedeutet "volles Profil".
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3t9r5p2m8q4"
down_revision: Union[str, None] = "b2s8q4n1l6p7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "oauth_states",
        sa.Column(
            "scope_profil", sa.String(length=20), nullable=True,
            comment="voll | mitarbeiter. NULL = voll (Inhaber-Pfad).",
        ),
    )


def downgrade() -> None:
    op.drop_column("oauth_states", "scope_profil")
