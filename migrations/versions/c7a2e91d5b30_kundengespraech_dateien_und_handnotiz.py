"""Kundengespraech: Handnotiz, angehaengte Bilder, Visualisierungs-Rueckweg

Revision ID: c7a2e91d5b30
Revises: f5b1c9d27a84
Create Date: 2026-07-30 17:10:00.000000

Macht aus der reinen Aufnahme-Liste einen Arbeitsbereich „Kundengespraech":

* ``gespraech_dateien`` — Zuordnung Bild → Gespraech. Die Bytes bleiben,
  wo sie hingehoeren: Fotos im Drive-Kundenordner (nur Datei-ID + Link
  hier), Visualisierungen in ``visualisierungen.result_image_data``.
  Loescht die Aufbewahrungsfrist spaeter das Gespraech, verschwindet nur
  die Zuordnung — das Foto bleibt im Kundenordner auffindbar.
* ``kundengespraeche.handnotiz`` — die getippte Notiz des Handwerkers,
  bewusst getrennt von der KI-Zusammenfassung und INTERN.
* ``visualisierungen.gespraech_id`` — der Rueckweg: im Gespraech
  gestartet, im Q-Chat gerendert, danach wieder im Gespraech sichtbar.

Rein additiv, kein Backfill.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c7a2e91d5b30"
down_revision: Union[str, None] = "f5b1c9d27a84"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "gespraech_dateien",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("gespraech_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("typ", sa.String(length=20), nullable=False),
        sa.Column("drive_file_id", sa.String(length=200), nullable=True),
        sa.Column("drive_url", sa.String(length=1000), nullable=True),
        sa.Column("visualisierung_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("dateiname", sa.String(length=300), nullable=True),
        sa.Column("mime", sa.String(length=100), nullable=True),
        # Base setzt created_at/updated_at auf allen Modellen — die
        # Migration muss sie mitbringen, sonst kippt der erste INSERT.
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["gespraech_id"], ["kundengespraeche.id"], ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["visualisierung_id"], ["visualisierungen.id"], ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_gespraech_dateien_tenant_id", "gespraech_dateien", ["tenant_id"],
    )
    op.create_index(
        "ix_gespraech_dateien_gespraech_id", "gespraech_dateien", ["gespraech_id"],
    )

    op.add_column(
        "kundengespraeche",
        sa.Column("handnotiz", sa.Text(), nullable=True),
    )
    op.add_column(
        "visualisierungen",
        sa.Column("gespraech_id", sa.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_visualisierungen_gespraech_id",
        "visualisierungen", "kundengespraeche",
        ["gespraech_id"], ["id"], ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_visualisierungen_gespraech_id", "visualisierungen", type_="foreignkey",
    )
    op.drop_column("visualisierungen", "gespraech_id")
    op.drop_column("kundengespraeche", "handnotiz")
    op.drop_index("ix_gespraech_dateien_gespraech_id", table_name="gespraech_dateien")
    op.drop_index("ix_gespraech_dateien_tenant_id", table_name="gespraech_dateien")
    op.drop_table("gespraech_dateien")
