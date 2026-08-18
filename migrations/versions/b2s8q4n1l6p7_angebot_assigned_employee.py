"""Auftrag einem Mitarbeiter zuweisen

Revision ID: b2s8q4n1l6p7
Revises: a1r7p3m9k2n5
Create Date: 2026-08-18 13:00:00.000000

`Angebot` IST die Auftrags-Entitaet (ab Status rechnung_erstellt) und war
die einzige zentrale Tabelle ohne Zuweisungsfeld — Kundengespraech,
Anfrage, EmailConversation, Rueckruf und Rechnung haben es laengst.
Ohne dieses Feld gibt es kein "nur meine Auftraege".

Backfill: alle Bestandsauftraege gehen an den Inhaber (is_default) des
jeweiligen Betriebs. Bewusst nicht NULL lassen — sonst waeren sie fuer
jeden ohne `auftraege.alle_sehen` unsichtbar, und der Inhaber-Screen
saehe nach dem Deploy anders aus als vorher. NULL bedeutet ab jetzt
"niemandem zugewiesen" und ist fuer eingeschraenkte Nutzer nicht
sichtbar (fail-closed).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b2s8q4n1l6p7"
down_revision: Union[str, None] = "a1r7p3m9k2n5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "angebote",
        sa.Column(
            "assigned_employee_id", postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="Wer fuehrt diesen Auftrag aus. NULL = niemandem "
                    "zugewiesen (fuer eingeschraenkte Nutzer unsichtbar).",
        ),
    )
    # SET NULL statt CASCADE: scheidet ein Mitarbeiter aus, bleibt der
    # Auftrag bestehen und faellt zurueck in "nicht zugewiesen" — genau
    # wie bei auftrag_stunden.employee_id.
    op.create_foreign_key(
        "fk_angebot_assigned_employee", "angebote", "employees",
        ["assigned_employee_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index(
        "ix_angebot_assigned", "angebote", ["tenant_id", "assigned_employee_id"],
    )

    # Bestandsauftraege dem Inhaber zuordnen.
    op.execute("""
        UPDATE angebote a
           SET assigned_employee_id = (
               SELECT e.id FROM employees e
                WHERE e.tenant_id = a.tenant_id AND e.is_default
                LIMIT 1
           )
         WHERE a.assigned_employee_id IS NULL
    """)


def downgrade() -> None:
    op.drop_index("ix_angebot_assigned", table_name="angebote")
    op.drop_constraint("fk_angebot_assigned_employee", "angebote", type_="foreignkey")
    op.drop_column("angebote", "assigned_employee_id")
