"""Auftragsprozess (eigene Zwischenschritte) + Auftrags-Archiv im Drive

Revision ID: f5b1c9d27a84
Revises: e384323f5d85
Create Date: 2026-07-30 10:00:00.000000

Legt zwei Tabellen an:

* ``auftrag_prozess_schritte`` — die tenant-eigenen Zwischenschritte des
  Auftragsprozesses. Verankert am Kern-Lifecycle ueber
  ``nach_kern_status`` + ``sort_index``; die fuenf Kern-Schritte selbst
  stehen weiterhin im Code (AUFTRAG_LIFECYCLE) und sind unveraenderlich.
* ``auftrag_schritt_status`` — pro Auftrag (Angebot) das Abhaken eines
  eigenen Schritts. Zeile da = erledigt.

Dazu drei additive Spalten auf ``angebote`` fuer den Abschluss und das
Drive-Archiv. Bestandszeilen bekommen NULL: bereits versendete Rechnungen
werden NICHT rueckwirkend archiviert (das wuerde beim ersten Deploy
Dutzende Drive-Uploads auf einen Schlag ausloesen); sie erscheinen in der
Liste der abgeschlossenen Auftraege trotzdem, nur ohne Archiv-Link.

Rein additiv, kein Backfill, kein Datenverlust beim Downgrade ausser den
neuen Tabellen/Spalten selbst.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f5b1c9d27a84"
down_revision: Union[str, None] = "e384323f5d85"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "auftrag_prozess_schritte",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("label", sa.String(length=60), nullable=False),
        sa.Column("nach_kern_status", sa.String(length=50), nullable=True),
        sa.Column("sort_index", sa.Integer(), nullable=False, server_default="0"),
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
    )
    op.create_index(
        "ix_auftrag_prozess_schritte_tenant_id",
        "auftrag_prozess_schritte", ["tenant_id"],
    )
    op.create_index(
        "ix_auftrag_prozess_schritte_order",
        "auftrag_prozess_schritte",
        ["tenant_id", "nach_kern_status", "sort_index"],
    )

    op.create_table(
        "auftrag_schritt_status",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("angebot_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("schritt_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("erledigt_am", sa.DateTime(timezone=True), nullable=False),
        sa.Column("erledigt_von_employee_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["angebot_id"], ["angebote.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["schritt_id"], ["auftrag_prozess_schritte.id"], ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["erledigt_von_employee_id"], ["employees.id"], ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "angebot_id", "schritt_id", name="uq_auftrag_schritt_status",
        ),
    )
    op.create_index(
        "ix_auftrag_schritt_status_tenant_id",
        "auftrag_schritt_status", ["tenant_id"],
    )
    op.create_index(
        "ix_auftrag_schritt_status_angebot_id",
        "auftrag_schritt_status", ["angebot_id"],
    )
    op.create_index(
        "ix_auftrag_schritt_status_schritt_id",
        "auftrag_schritt_status", ["schritt_id"],
    )

    op.add_column(
        "angebote",
        sa.Column("abgeschlossen_am", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "angebote",
        sa.Column("archiv_drive_folder_id", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "angebote",
        sa.Column("archiv_drive_folder_url", sa.String(length=500), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("angebote", "archiv_drive_folder_url")
    op.drop_column("angebote", "archiv_drive_folder_id")
    op.drop_column("angebote", "abgeschlossen_am")

    op.drop_index("ix_auftrag_schritt_status_schritt_id", "auftrag_schritt_status")
    op.drop_index("ix_auftrag_schritt_status_angebot_id", "auftrag_schritt_status")
    op.drop_index("ix_auftrag_schritt_status_tenant_id", "auftrag_schritt_status")
    op.drop_table("auftrag_schritt_status")

    op.drop_index("ix_auftrag_prozess_schritte_order", "auftrag_prozess_schritte")
    op.drop_index("ix_auftrag_prozess_schritte_tenant_id", "auftrag_prozess_schritte")
    op.drop_table("auftrag_prozess_schritte")
