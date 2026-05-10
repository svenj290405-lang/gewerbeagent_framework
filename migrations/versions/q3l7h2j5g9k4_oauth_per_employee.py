"""oauth_tokens.employee_id + oauth_states.employee_slug (Multi-OAuth pro Employee)

Revision ID: q3l7h2j5g9k4
Revises: p9k6g1i4f8h2
Create Date: 2026-05-10 22:00:00.000000

Phase 1 der Multi-Mitarbeiter-Erweiterung (Plan: das-machen-wir-gleich-
foamy-frost.md). Heute hat OAuthToken einen UNIQUE-Constraint
(tenant_id, provider) — heisst max 1 Google + 1 Microsoft pro Tenant.
Wenn ein Tenant 5 Mitarbeiter hat die jeder eigenen Google-Account
verbinden will, geht das nicht.

Strategie (Two-Step zum Constraint-Drop):
- M1 (DIESE): employee_id-Spalte + 2 partial-unique Indizes parallel
  zum alten Constraint. Backfill: alle existierenden Tokens →
  Default-Employee des jeweiligen Tenants. Code wird so angepasst dass
  er beide Pfade versteht.
- M2 (separate Migration in q4...): alten uq_tenant_provider droppen.
  Erst nachdem Code eine Woche stabil mit beiden Pfaden lief.

Schema:
- oauth_tokens.employee_id UUID NULL FK -> employees.id ON DELETE
  CASCADE. NULL = Tenant-weiter Legacy-Token (nicht mehr neu vergeben,
  nur fuer Backward-Compat-Lookup).
- 2 partial-unique-Indizes:
  * uq_oauth_tenant_provider_when_no_employee
    UNIQUE (tenant_id, provider) WHERE employee_id IS NULL
  * uq_oauth_employee_provider
    UNIQUE (employee_id, provider) WHERE employee_id IS NOT NULL

oauth_states.employee_slug:
- Wenn ein Mitarbeiter ueber /kalender_verbinden seinen OAuth startet,
  muss der Callback wissen FUER WEN der Token gelagert werden soll.
  Der state-Token im OAuth-Flow speichert das mit.
- Spalte ist nullable (Default = ohne Slug → faellt auf Default-Emp).

Backfill: alle bestehenden oauth_tokens auf Default-Employee des
Tenants. Falls ein Tenant keinen Default-Employee hat (nicht
moeglich nach Phase 0): Token bleibt NULL und ist via Legacy-Index
ueber tenant-weite UNIQUE addressbar.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "q3l7h2j5g9k4"
down_revision = "p9k6g1i4f8h2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- oauth_tokens erweitern ---
    op.add_column(
        "oauth_tokens",
        sa.Column(
            "employee_id",
            UUID(as_uuid=True),
            sa.ForeignKey("employees.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_oauth_tokens_employee_id", "oauth_tokens", ["employee_id"],
    )

    # Backfill: alle bestehenden Tokens → Default-Employee des Tenants
    op.execute("""
        UPDATE oauth_tokens ot
        SET employee_id = e.id
        FROM employees e
        WHERE e.tenant_id = ot.tenant_id AND e.is_default
    """)

    # --- 2 partial-unique-Indizes ---
    # Tenant-weiter Legacy-Index (NULL employee_id)
    op.create_index(
        "uq_oauth_tenant_provider_when_no_employee",
        "oauth_tokens",
        ["tenant_id", "provider"],
        unique=True,
        postgresql_where=sa.text("employee_id IS NULL"),
    )
    # Employee-spezifischer Index
    op.create_index(
        "uq_oauth_employee_provider",
        "oauth_tokens",
        ["employee_id", "provider"],
        unique=True,
        postgresql_where=sa.text("employee_id IS NOT NULL"),
    )

    # --- oauth_states.employee_slug ---
    op.add_column(
        "oauth_states",
        sa.Column("employee_slug", sa.String(length=64), nullable=True),
    )

    # Note: alter Constraint uq_tenant_provider bleibt fuer Phase M2 erhalten
    # (separate Migration). Damit ist der Datenbestand DOPPELT gegen
    # Konflikte gesichert solange beide Code-Pfade aktiv sind.


def downgrade() -> None:
    op.drop_column("oauth_states", "employee_slug")
    op.drop_index("uq_oauth_employee_provider", table_name="oauth_tokens")
    op.drop_index(
        "uq_oauth_tenant_provider_when_no_employee", table_name="oauth_tokens",
    )
    op.drop_index("ix_oauth_tokens_employee_id", table_name="oauth_tokens")
    op.drop_column("oauth_tokens", "employee_id")
