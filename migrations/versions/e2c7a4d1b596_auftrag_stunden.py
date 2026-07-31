"""Auftrag: geleistete Arbeitsstunden je Mitarbeiter

Revision ID: e2c7a4d1b596
Revises: d9b4f2a7c081
Create Date: 2026-07-31 12:05:00.000000

Am Fortschrittsregler traegt der Handwerker ein, wie lange er an einem
Auftrag gearbeitet hat; am Auftrag steht danach, wer wie viele Stunden
geleistet hat (Nachkalkulation + Grundlage fuer die Abrechnung).

``employee_name`` liegt als Schnappschuss neben ``employee_id``: der
Fremdschluessel ist SET NULL, damit ein entfernter Mitarbeiter die
Stunden des Auftrags nicht mitnimmt — der Name muss dann trotzdem noch
lesbar sein.

Die CHECK-Constraint (0 < stunden <= 24) faengt die verrutschte
Kommastelle auf DB-Ebene ab, nicht nur im Formular.

Rein additiv, kein Backfill.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e2c7a4d1b596"
down_revision: Union[str, None] = "d9b4f2a7c081"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "auftrag_stunden",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("angebot_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("employee_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("employee_name", sa.String(length=200), nullable=False),
        sa.Column("stunden", sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column("datum", sa.Date(), nullable=False),
        sa.Column("notiz", sa.String(length=300), nullable=True),
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
        sa.ForeignKeyConstraint(["angebot_id"], ["angebote.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "stunden > 0 AND stunden <= 24", name="ck_auftrag_stunden_bereich",
        ),
    )
    op.create_index("ix_auftrag_stunden_tenant_id", "auftrag_stunden", ["tenant_id"])
    op.create_index("ix_auftrag_stunden_angebot_id", "auftrag_stunden", ["angebot_id"])
    op.create_index("ix_auftrag_stunden_employee_id", "auftrag_stunden", ["employee_id"])
    op.create_index(
        "ix_auftrag_stunden_angebot_datum", "auftrag_stunden", ["angebot_id", "datum"],
    )


def downgrade() -> None:
    op.drop_index("ix_auftrag_stunden_angebot_datum", table_name="auftrag_stunden")
    op.drop_index("ix_auftrag_stunden_employee_id", table_name="auftrag_stunden")
    op.drop_index("ix_auftrag_stunden_angebot_id", table_name="auftrag_stunden")
    op.drop_index("ix_auftrag_stunden_tenant_id", table_name="auftrag_stunden")
    op.drop_table("auftrag_stunden")
