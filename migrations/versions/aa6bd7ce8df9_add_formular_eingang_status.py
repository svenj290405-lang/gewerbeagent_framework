"""anfrage_responses.bearbeitungs_status + bearbeitet_at + bearbeitet_by

Revision ID: aa6bd7ce8df9
Revises: z5ac6od8p1q3
Create Date: 2026-05-17 13:00:00.000000

Damit Formular-Antworten nicht unter den Tisch fallen: jede Response
bekommt einen expliziten Bearbeitungs-Status, den der Handwerker via
Inline-Buttons im Telegram-Push setzt. Der Daily-Heartbeat-Cron checkt
dann morgens ob Antworten > 12h auf 'neu' stehen und pingt nach.

Status-Werte (CHECK constraint, nicht als ENUM wegen einfacherer
Migration):
- 'neu'             — frisch eingegangen, noch nicht angesehen
- 'in_bearbeitung'  — Handwerker hat angefangen, hat aber noch nicht
                      finalisiert (Angebot in Lexware, Termin ausgemacht
                      etc.)
- 'erledigt'        — abgeschlossen (Angebot raus / Kunde reagiert)
- 'abgelehnt'       — Spam / nicht passend / Kunde hat zurueckgezogen

bearbeitet_at + bearbeitet_by_employee_id sind Audit-Felder: wer hat
den Status zuletzt geaendert. Bei status='neu' beide NULL.

Partial-Index ix_anfrage_response_offen beschleunigt den Heartbeat-
Query "alle offenen Antworten pro Tenant" — die Tabelle waechst
linear mit Anfragen, die meisten landen schnell auf 'erledigt'.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "aa6bd7ce8df9"
# Merge zweier paralleler Heads (z5ac6od8p1q3 + d2n6q8s4t1v6) zu einem
# linearen Verlauf — der Drive-Root-Folder-Branch wurde in einem anderen
# Strang erstellt und faengt diese Migration jetzt mit ein.
down_revision: Union[str, Sequence[str]] = ("z5ac6od8p1q3", "d2n6q8s4t1v6")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "anfrage_responses",
        sa.Column(
            "bearbeitungs_status",
            sa.String(length=20),
            nullable=False,
            server_default="neu",
        ),
    )
    op.add_column(
        "anfrage_responses",
        sa.Column(
            "bearbeitet_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "anfrage_responses",
        sa.Column(
            "bearbeitet_by_employee_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("employees.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_anfrage_response_status",
        "anfrage_responses",
        "bearbeitungs_status IN ('neu','in_bearbeitung','erledigt','abgelehnt')",
    )
    op.create_index(
        "ix_anfrage_response_offen",
        "anfrage_responses",
        ["submitted_at"],
        postgresql_where=sa.text(
            "bearbeitungs_status IN ('neu','in_bearbeitung')"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_anfrage_response_offen", table_name="anfrage_responses",
    )
    op.drop_constraint(
        "ck_anfrage_response_status", "anfrage_responses", type_="check",
    )
    op.drop_column("anfrage_responses", "bearbeitet_by_employee_id")
    op.drop_column("anfrage_responses", "bearbeitet_at")
    op.drop_column("anfrage_responses", "bearbeitungs_status")
