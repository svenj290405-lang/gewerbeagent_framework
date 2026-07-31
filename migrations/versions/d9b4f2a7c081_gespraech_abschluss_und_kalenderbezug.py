"""Kundengespraech: Abschluss (Drive-Protokoll) und Kalender-Bezug

Revision ID: d9b4f2a7c081
Revises: c7a2e91d5b30
Create Date: 2026-07-31 10:20:00.000000

Das Gespraech bekommt ein Ende: „fertig — beim Kunden einpflegen" legt den
Kunden an (falls noch keiner da ist) und schreibt Protokoll + Bilder in den
Drive-Kundenordner. Was dabei entsteht, wird hier festgehalten:

* ``abgeschlossen_am`` — wann eingepflegt wurde (Status ``abgeschlossen``).
* ``protokoll_drive_file_id`` / ``protokoll_drive_url`` — das Gespraechs-
  protokoll im Kundenordner. Bleiben leer, wenn Drive nicht verbunden war;
  der Abschluss gilt trotzdem.
* ``kalender_event_id`` — aus welchem geplanten Kalendertermin das
  Gespraech entstanden ist. Damit taucht ein Termin, zu dem schon ein
  Gespraech laeuft, nicht noch einmal als Vorschlag auf.

Rein additiv, kein Backfill. „Verworfene" Gespraeche brauchen keine Spalte:
sie bekommen den bestehenden Status ``abgelehnt`` (Soft-Delete).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d9b4f2a7c081"
down_revision: Union[str, None] = "c7a2e91d5b30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "kundengespraeche",
        sa.Column("kalender_event_id", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "kundengespraeche",
        sa.Column("abgeschlossen_am", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "kundengespraeche",
        sa.Column("protokoll_drive_file_id", sa.String(length=200), nullable=True),
    )
    op.add_column(
        "kundengespraeche",
        sa.Column("protokoll_drive_url", sa.String(length=1000), nullable=True),
    )
    op.create_index(
        "ix_kundengespraeche_kalender_event_id",
        "kundengespraeche", ["kalender_event_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_kundengespraeche_kalender_event_id", table_name="kundengespraeche",
    )
    op.drop_column("kundengespraeche", "protokoll_drive_url")
    op.drop_column("kundengespraeche", "protokoll_drive_file_id")
    op.drop_column("kundengespraeche", "abgeschlossen_am")
    op.drop_column("kundengespraeche", "kalender_event_id")
