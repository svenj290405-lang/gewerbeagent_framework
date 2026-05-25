"""M2: alten uq_tenant_provider-Constraint droppen (Multi-OAuth pro Employee)

Revision ID: s5n9j4l7q3r8
Revises: c1e4a7b9d2f5
Create Date: 2026-05-25 09:00:00.000000

Phase M2 der Multi-Mitarbeiter-OAuth-Umstellung (Fortsetzung von M1,
Revision q3l7h2j5g9k4). M1 hat oauth_tokens.employee_id + zwei
partial-unique-Indizes angelegt, den alten Constraint
uq_tenant_provider (tenant_id, provider) aber als Sicherheitsnetz
behalten.

Der Code schreibt inzwischen stabil employee_id-aware
(_upsert_oauth_token / handle_callback). Damit blockiert
uq_tenant_provider nur noch echten Mehrwert: ein zweiter Mitarbeiter
kann seinen EIGENEN Google-/Outlook-Kalender nicht verbinden, weil der
Insert (tenant_id, provider) trotz unterschiedlicher employee_id gegen
den Constraint laeuft (UniqueViolationError → "Verknuepfung
fehlgeschlagen").

Drop des Constraints. Eindeutigkeit bleibt voll gewahrt durch die zwei
partial-Indizes aus M1:
  - uq_oauth_employee_provider
      UNIQUE (employee_id, provider) WHERE employee_id IS NOT NULL
  - uq_oauth_tenant_provider_when_no_employee
      UNIQUE (tenant_id, provider) WHERE employee_id IS NULL
"""
from alembic import op


revision = "s5n9j4l7q3r8"
down_revision = "c1e4a7b9d2f5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uq_tenant_provider", "oauth_tokens", type_="unique")


def downgrade() -> None:
    # Achtung: Re-Anlage schlaegt fehl, falls inzwischen zwei Mitarbeiter
    # desselben Tenants denselben Provider verbunden haben (genau der Fall,
    # den M2 erlaubt). Downgrade nur sinnvoll bei leerem/single-Employee-Stand.
    op.create_unique_constraint(
        "uq_tenant_provider", "oauth_tokens", ["tenant_id", "provider"],
    )
