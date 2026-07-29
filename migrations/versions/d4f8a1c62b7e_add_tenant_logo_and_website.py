"""Tenant-Branding: Firmenlogo + Website-Link

Fuer die Kopfzeile der WebApp: neben dem Firmennamen erscheinen Logo und
Website-Link; ein Klick aufs Logo oeffnet die Firmen-Website.

Logo liegt als BYTEA in der Tabelle (Muster wie visualisierungen.*_data):
klein, atomar, faellt mit dem Tenant weg. Kein Drive-/Dateisystem-Umweg,
der aus dem Takt geraten koennte.

Additive-only: drei neue nullable Spalten, keine Aenderung an Bestehendem.

Revision ID: d4f8a1c62b7e
Revises: a1bd7ef2c904
"""
from alembic import op
import sqlalchemy as sa


revision = "d4f8a1c62b7e"
down_revision = "a1bd7ef2c904"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("website_url", sa.String(300), nullable=True))
    op.add_column("tenants", sa.Column("logo_data", sa.LargeBinary(), nullable=True))
    op.add_column("tenants", sa.Column("logo_mime", sa.String(50), nullable=True))


def downgrade() -> None:
    op.drop_column("tenants", "logo_mime")
    op.drop_column("tenants", "logo_data")
    op.drop_column("tenants", "website_url")
