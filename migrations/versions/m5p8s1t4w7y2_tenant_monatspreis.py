"""tenants: Monatspreis und Abrechnungsbeginn

Revision ID: m5p8s1t4w7y2
Revises: k3m6p9r2t5w8
Create Date: 2026-08-24 00:05:00.000000

Bisher kannte das System nur die Kostenseite (api_usage_log). Was ein
Betrieb zahlt, stand nirgends — eine Marge liess sich also gar nicht
zeigen. Zwei Felder statt eines Abo-Modells: bei einer Handvoll Kunden
ist eine Zahl die ehrliche Antwort.
"""
from alembic import op
import sqlalchemy as sa


revision = "m5p8s1t4w7y2"
down_revision = "k3m6p9r2t5w8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column(
        "monatspreis_eur", sa.Numeric(10, 2), nullable=True))
    op.add_column("tenants", sa.Column(
        "abrechnung_seit", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("tenants", "abrechnung_seit")
    op.drop_column("tenants", "monatspreis_eur")
