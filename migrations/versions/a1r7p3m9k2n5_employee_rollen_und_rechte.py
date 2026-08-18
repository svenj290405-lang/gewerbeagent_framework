"""Mitarbeiter-Rollen + abweichende Einzelrechte

Revision ID: a1r7p3m9k2n5
Revises: f4b7d2e9a1c8
Create Date: 2026-08-18 12:00:00.000000

Bis hierher gab es genau EIN Rechtebit: employees.is_default. Ein frisch
angelegter Monteur sah damit alle Umsaetze, offenen Posten, Kundendaten,
Gespraechstranskripte und die App-Nutzung seiner Kollegen — 80 der 115
PWA-Routen lieferten tenant-weite Daten ohne jede Sichtbarkeitsgrenze.

Diese Migration legt nur das Datenfundament. Sie aendert das Verhalten der
App NICHT: die Rechte werden erst in einer spaeteren Stufe durchgesetzt
(dort zuerst im Trockenlauf, der nur loggt).

Zwei Teile:

1. employees.role — 'inhaber' | 'buero' | 'monteur'. Vorlage fuer die
   effektiven Rechte, aufgeloest in core/features/permission_check.py.
   is_default bleibt unangetastet; es traegt weiterhin die Bedeutungen
   "Legacy-Mirror-Anker" und "Notification-Fallback".

2. employee_permissions — sparse Abweichungen vom Rollen-Preset, eine
   Zeile pro (Mitarbeiter, Recht). Fehlt die Zeile, gilt das Preset.
   Bewusst sparse statt Snapshot: kommt spaeter ein Recht dazu, erben
   alle Buerokraefte es automatisch in der Preset-Auspraegung, ohne dass
   eine Datenmigration noetig waere.

Backfill-Regel fuer Bestandsmitarbeiter (bewusste Produktentscheidung):
is_default -> 'inhaber', alle uebrigen -> 'buero'. Bestehende Mitarbeiter
sollen nicht ueber Nacht vor leeren Screens stehen; bei ihnen faellt
gegenueber heute nur die Buchhaltung weg. Neu angelegte Mitarbeiter
bekommen dagegen den restriktiven server_default 'monteur'.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a1r7p3m9k2n5"
down_revision: Union[str, None] = "f4b7d2e9a1c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---- 1. Rolle am Mitarbeiter --------------------------------
    op.add_column(
        "employees",
        sa.Column(
            "role", sa.String(length=20), nullable=False,
            server_default="monteur",
            comment="Rechte-Rolle: inhaber | buero | monteur",
        ),
    )

    # Bestandsdaten: Inhaber bleibt Inhaber, alle anderen werden Buero.
    op.execute("UPDATE employees SET role = 'inhaber' WHERE is_default")
    op.execute("UPDATE employees SET role = 'buero' WHERE NOT is_default")

    op.create_check_constraint(
        "ck_employee_role",
        "employees",
        "role IN ('inhaber','buero','monteur')",
    )
    op.create_index(
        "ix_emp_tenant_role", "employees", ["tenant_id", "role"],
    )

    # ---- 2. Abweichende Einzelrechte ----------------------------
    # created_at/updated_at kommen von core.database.base.Base und muessen
    # hier mit angelegt werden — sonst bricht jedes ORM-Insert.
    op.create_table(
        "employee_permissions",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False,
        ),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), nullable=False,
        ),
        sa.Column(
            "employee_id", postgresql.UUID(as_uuid=True), nullable=False,
        ),
        sa.Column(
            "permission_key", sa.String(length=50), nullable=False,
            comment="Key aus core/features/permissions.py",
        ),
        sa.Column("allowed", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"], ["employees.id"], ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "employee_id", "permission_key", name="uq_emp_permission",
        ),
    )
    op.create_index(
        "ix_employee_permissions_employee_id",
        "employee_permissions", ["employee_id"],
    )
    op.create_index(
        "ix_emp_perm_tenant", "employee_permissions", ["tenant_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_emp_perm_tenant", table_name="employee_permissions")
    op.drop_index(
        "ix_employee_permissions_employee_id",
        table_name="employee_permissions",
    )
    op.drop_table("employee_permissions")

    op.drop_index("ix_emp_tenant_role", table_name="employees")
    op.drop_constraint("ck_employee_role", "employees", type_="check")
    op.drop_column("employees", "role")
