"""employee.calendar_provider Spalte (google | microsoft)

Revision ID: p9k6g1i4f8h2
Revises: o8j5f0h3e7g1
Create Date: 2026-05-10 21:00:00.000000

Vorbereitung fuer Outlook-Calendar-Support: jeder Mitarbeiter waehlt
beim Onboarding ob sein Kalender bei Google oder Microsoft liegt.
plugins/kalender dispatched dann an die richtige API (Google Calendar
v3 vs Microsoft Graph /me/calendar).

Felder:
- calendar_provider: 'google' | 'microsoft' | NULL (= noch nicht
  eingerichtet, /kalender_verbinden Wizard noch nicht durchlaufen).
  Default 'google' als Backfill weil bisher alles auf Google lief.
- calendar_id: optionaler externer Identifier (Google: 'primary' o.ae.,
  Microsoft: id einer Calendar-Resource). NULL = primaerer Default.
  In tool_configs.kalender steckt heute calendar_id pro Tenant — wird
  zukuenftig hier pro Mitarbeiter ueberschrieben.

Backfill: alle existierenden Employees → 'google' (Annahme: heutige
Setups sind Google). Sven kann manuell auf 'microsoft' aendern wenn
ein Mitarbeiter neu konfiguriert wird.

Backward-Compat: NULL ist erlaubt und bedeutet "Kalender noch nicht
eingerichtet" — Slot-Suche skippt diesen Employee dann (Phase-5-
Router faellt auf Default zurueck).
"""
from alembic import op
import sqlalchemy as sa


revision = "p9k6g1i4f8h2"
down_revision = "o8j5f0h3e7g1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "employees",
        sa.Column("calendar_provider", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "employees",
        sa.Column("calendar_id", sa.String(length=255), nullable=True),
    )
    # Backfill: bisherige Employees auf 'google' (Status Quo)
    op.execute("UPDATE employees SET calendar_provider = 'google'")
    # Index auf provider — Routing-Queries koennen daraus profitieren
    op.create_index(
        "ix_employees_calendar_provider", "employees", ["calendar_provider"],
    )


def downgrade() -> None:
    op.drop_index("ix_employees_calendar_provider", table_name="employees")
    op.drop_column("employees", "calendar_id")
    op.drop_column("employees", "calendar_provider")
