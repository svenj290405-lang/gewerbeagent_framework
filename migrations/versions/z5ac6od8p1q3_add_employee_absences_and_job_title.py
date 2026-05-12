"""employee_absences-Tabelle + employees.job_title + reschedule-Mail-Spalten

Revision ID: z5ac6od8p1q3
Revises: y4ab5nc7o8p1
Create Date: 2026-05-12 12:00:00.000000

Phase 6: Krank/Urlaub-Management mit automatischer Termin-Umverteilung.

Neue Spalten:
- employees.job_title: Freitext-Rolle ("Geselle", "Lehrling", "Inhaber",
  "Subunternehmer"). Reiner Anzeige-Wert, kein Permission-Effekt.
- kundengespraeche.reschedule_mail_message_id + reschedule_mail_conversation_id:
  Reply-Tracking fuer die Verschiebungs-Mail die wir an den Kunden senden
  wenn sein Termin wegen Krankheit auf einen anderen Mitarbeiter wandert.
  Pattern identisch zu angebot_*-Mail-Tracking.

Neue Tabelle employee_absences:
  Eintrag pro Krank-/Urlaubsmeldung. start_date Pflicht, end_date NULL
  = open-ended (z.B. "Max ist krank, weiss noch nicht wie lange").
  absence_type ist 'krank' | 'urlaub' | 'sonstiges'. notes optional.

Idempotenz der Auto-Umverteilung laeuft ueber den existierenden
assigned_employee_id-Check am Kundengespraech, nicht ueber ein
processed_at-Flag hier — das spart eine Spalte und ist 1:1
konsistent zur Wirklichkeit.

Additiv. Backfill nicht noetig (job_title NULL, Tabelle leer).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "z5ac6od8p1q3"
down_revision: Union[str, None] = "y4ab5nc7o8p1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1) employees.job_title
    op.add_column(
        "employees",
        sa.Column("job_title", sa.String(length=100), nullable=True),
    )

    # 2) kundengespraeche reschedule-Mail-Tracking
    op.add_column(
        "kundengespraeche",
        sa.Column(
            "reschedule_mail_message_id", sa.String(length=500), nullable=True
        ),
    )
    op.add_column(
        "kundengespraeche",
        sa.Column(
            "reschedule_mail_conversation_id", sa.String(length=500),
            nullable=True,
        ),
    )

    # 3) employee_absences
    op.create_table(
        "employee_absences",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "employee_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("employees.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("absence_type", sa.String(length=20), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_by_employee_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("employees.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "absence_type IN ('krank','urlaub','sonstiges')",
            name="ck_absence_type",
        ),
    )
    op.create_index(
        "ix_absence_employee_start",
        "employee_absences",
        ["employee_id", "start_date"],
    )
    op.create_index(
        "ix_absence_tenant_active",
        "employee_absences",
        ["tenant_id", "start_date", "end_date"],
    )
    # Partial-Index: schnell finden welche Absences noch offen sind
    op.create_index(
        "ix_absence_open_ended",
        "employee_absences",
        ["employee_id"],
        postgresql_where=sa.text("end_date IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_absence_open_ended", table_name="employee_absences")
    op.drop_index("ix_absence_tenant_active", table_name="employee_absences")
    op.drop_index("ix_absence_employee_start", table_name="employee_absences")
    op.drop_table("employee_absences")
    op.drop_column("kundengespraeche", "reschedule_mail_conversation_id")
    op.drop_column("kundengespraeche", "reschedule_mail_message_id")
    op.drop_column("employees", "job_title")
