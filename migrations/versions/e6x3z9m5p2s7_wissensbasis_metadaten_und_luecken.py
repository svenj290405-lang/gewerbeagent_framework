"""Wissensbasis: Metadaten (Quelle/Sichtbarkeit/Aktiv/Bestaetigung) + Wissensluecken

Revision ID: e6x3z9m5p2s7
Revises: d5w2y8k4n6q1
Create Date: 2026-08-23 10:00:00.000000

Zwei Aenderungen:

1. ``tenant_knowledge`` bekommt Metadaten. Bisher war ein Eintrag nur
   (Kategorie, Freitext) — man sah weder woher er kam, noch ob er noch
   gilt, und ALLES landete ungefiltert im Voice-System-Prompt (also bei
   ElevenLabs) und in jeder Kundenmail. Neu:
     - ``quelle``       — mensch / q / template / import (Herkunft)
     - ``sichtbarkeit`` — kunde / intern; nur ``kunde`` geht an Voice+Mail
     - ``aktiv``        — Eintrag stilllegen ohne ihn zu loeschen
     - ``zuletzt_bestaetigt_am`` — fuer den Frische-Ping ("gilt der Preis noch?")

   Backfill: ``zuletzt_bestaetigt_am`` = ``created_at`` der Bestandszeilen,
   sonst wuerde der Frische-Ping am ersten Tag alles auf einmal anmahnen.

2. Neue Tabelle ``wissensluecken``. Bisher wurde eine Kundenfrage, die die
   Wissensbasis nicht beantworten konnte, nur ins Logfile geschrieben
   (``wissensbasis: ... treffer=0``) und war damit verloren. Jetzt wird sie
   persistiert, in der App als Aufgabe angezeigt und nach der Antwort
   direkt in einen ``tenant_knowledge``-Eintrag verwandelt.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e6x3z9m5p2s7"
down_revision: Union[str, Sequence[str], None] = "d5w2y8k4n6q1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- 1. tenant_knowledge: Metadaten ---------------------------------
    op.add_column(
        "tenant_knowledge",
        sa.Column(
            "quelle", sa.String(length=20),
            nullable=False, server_default="mensch",
        ),
    )
    op.add_column(
        "tenant_knowledge",
        sa.Column(
            "sichtbarkeit", sa.String(length=10),
            nullable=False, server_default="kunde",
        ),
    )
    op.add_column(
        "tenant_knowledge",
        sa.Column(
            "aktiv", sa.Boolean(),
            nullable=False, server_default=sa.text("true"),
        ),
    )
    op.add_column(
        "tenant_knowledge",
        sa.Column(
            "zuletzt_bestaetigt_am", sa.DateTime(timezone=True), nullable=True
        ),
    )
    # Bestandszeilen gelten als zuletzt bei Anlage bestaetigt.
    op.execute(
        "UPDATE tenant_knowledge "
        "SET zuletzt_bestaetigt_am = created_at "
        "WHERE zuletzt_bestaetigt_am IS NULL"
    )

    # --- 2. wissensluecken ----------------------------------------------
    # created_at/updated_at gehoeren dazu: core.database.base.Base haengt sie
    # an JEDES Modell — fehlen sie in der Migration, bricht das ORM beim
    # INSERT ... RETURNING auf einem frisch migrierten Schema.
    op.create_table(
        "wissensluecken",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("frage", sa.String(length=500), nullable=False),
        sa.Column("kanal", sa.String(length=20), nullable=False),
        sa.Column("kunde", sa.String(length=200), nullable=True),
        sa.Column(
            "status", sa.String(length=20),
            nullable=False, server_default="offen",
        ),
        sa.Column(
            "anzahl", sa.Integer(), nullable=False, server_default="1",
        ),
        sa.Column(
            "zuletzt_gefragt_am", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.Column("erledigt_am", sa.DateTime(timezone=True), nullable=True),
        sa.Column("knowledge_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE"
        ),
        # Wird der erzeugte Wissens-Eintrag spaeter geloescht, bleibt die
        # Luecke als Historie stehen (SET NULL statt CASCADE).
        sa.ForeignKeyConstraint(
            ["knowledge_id"], ["tenant_knowledge.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_wissensluecken_tenant_status",
        "wissensluecken",
        ["tenant_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_wissensluecken_tenant_status", table_name="wissensluecken")
    op.drop_table("wissensluecken")
    op.drop_column("tenant_knowledge", "zuletzt_bestaetigt_am")
    op.drop_column("tenant_knowledge", "aktiv")
    op.drop_column("tenant_knowledge", "sichtbarkeit")
    op.drop_column("tenant_knowledge", "quelle")
