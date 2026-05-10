"""assignee_employee_id auf email_conversations + kundengespraeche + rechnungen + anfrage_responses

Revision ID: o8j5f0h3e7g1
Revises: l5g2c9e7b8d4
Create Date: 2026-05-10 16:00:00.000000

Phase 4 der Multi-Mitarbeiter-Erweiterung
(`das-machen-wir-gleich-foamy-frost.md`).

Fuegt assigned_employee_id (und bei Kundengespraech auch
created_by_employee_id) zu den vier zentralen Workflow-Tabellen
hinzu. Phase-5-Skill-Router (kommt spaeter) setzt diese Felder beim
Inbound; Briefing-Befehle (Phase 4 ebenfalls) filtern danach pro
Mitarbeiter.

Felder (alle UUID NULL, FK auf employees.id, ON DELETE SET NULL —
deaktivierte Mitarbeiter sollen Termine nicht killen):
- email_conversations.assigned_employee_id
- kundengespraeche.assigned_employee_id
- kundengespraeche.created_by_employee_id (wer hat aufgenommen)
- rechnungen.responsible_employee_id
- anfrage_responses.assigned_employee_id

Backfill: bestehende Zeilen → Default-Employee des jeweiligen
Tenants. Damit kann der Skill-Router (Phase 5) saubere Logic
schreiben "wenn null → Default" ohne dass der Live-Tenant ohne
zugewiesene Termine dasteht.

Fuer anfrage_responses fehlt direktes tenant_id-Feld — Backfill
via Join ueber anfrage_tokens.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "o8j5f0h3e7g1"
down_revision = "l5g2c9e7b8d4"
branch_labels = None
depends_on = None


# Tabellen + Spalten in einer kleinen Tabelle, weil 5x Fast-identische
# Logic — einfacher zu lesen + zu erweitern.
ASSIGNEE_COLUMNS = [
    ("email_conversations", "assigned_employee_id"),
    ("kundengespraeche", "assigned_employee_id"),
    ("kundengespraeche", "created_by_employee_id"),
    ("rechnungen", "responsible_employee_id"),
]


def upgrade() -> None:
    # --- Spalten anlegen ---
    for table, col in ASSIGNEE_COLUMNS:
        op.add_column(
            table,
            sa.Column(
                col,
                UUID(as_uuid=True),
                sa.ForeignKey("employees.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
        op.create_index(
            f"ix_{table}_{col}", table, [col],
        )
    # anfrage_responses hat kein tenant_id — separate Behandlung
    op.add_column(
        "anfrage_responses",
        sa.Column(
            "assigned_employee_id",
            UUID(as_uuid=True),
            sa.ForeignKey("employees.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_anfrage_responses_assigned_employee_id",
        "anfrage_responses",
        ["assigned_employee_id"],
    )

    # --- Backfill: jeweils Default-Employee des Tenants ---
    conn = op.get_bind()
    for table, col in ASSIGNEE_COLUMNS:
        conn.execute(sa.text(f"""
            UPDATE {table} t
            SET {col} = e.id
            FROM employees e
            WHERE e.tenant_id = t.tenant_id AND e.is_default
              AND t.{col} IS NULL
        """))
    # anfrage_responses: tenant_id liegt am parent token
    conn.execute(sa.text("""
        UPDATE anfrage_responses ar
        SET assigned_employee_id = e.id
        FROM anfrage_tokens at, employees e
        WHERE ar.token_id = at.id
          AND e.tenant_id = at.tenant_id
          AND e.is_default
          AND ar.assigned_employee_id IS NULL
    """))


def downgrade() -> None:
    op.drop_index(
        "ix_anfrage_responses_assigned_employee_id", table_name="anfrage_responses",
    )
    op.drop_column("anfrage_responses", "assigned_employee_id")
    for table, col in reversed(ASSIGNEE_COLUMNS):
        op.drop_index(f"ix_{table}_{col}", table_name=table)
        op.drop_column(table, col)
