"""geocode_cache: created_at/updated_at nachruesten (Base-Spalten)

Revision ID: g8h2k4m7n1p5
Revises: f7y4a1n6q3t8
Create Date: 2026-08-23 21:55:00.000000

Dieselbe Luecke wie damals bei health_check_results (u8j2m5q9r3t6):
k4f1a8b2d6e3 legte `geocode_cache` ohne die Audit-Spalten an, die
core.database.base.Base jedem Modell automatisch anhaengt. Jeder
Voll-ORM-Zugriff (GeocodeCache(...) anlegen, select(GeocodeCache))
haette mit UndefinedColumnError abgebrochen.

Aufgefallen ist es bis heute nicht, weil Geocoding aus ist (keine
Maps-/ORS-Keys) und der naechtliche Cleanup nur Spalten-Queries
benutzt. Die Mine haette gezuendet, sobald jemand Geo aktiviert.
Tabelle ist leer, deshalb ist das Nachruesten unproblematisch.

Gefunden mit scripts/schema_drift_check.py, das genau diese Klasse
Fehler ab jetzt dauerhaft aufspuert.
"""
from alembic import op
import sqlalchemy as sa


revision = "g8h2k4m7n1p5"
down_revision = "f7y4a1n6q3t8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "geocode_cache",
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )
    op.add_column(
        "geocode_cache",
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("geocode_cache", "updated_at")
    op.drop_column("geocode_cache", "created_at")
