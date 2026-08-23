"""kunden: anonymized_at fuer die Loeschung auf Verlangen (Art. 17)

Revision ID: h9k3m6p2r5t8
Revises: g8h2k4m7n1p5
Create Date: 2026-08-23 22:10:00.000000

Die Kundenakte kann nicht einfach geloescht werden: an ihr haengen
Rechnungen und Auftraege, die aufbewahrt werden muessen (§ 147 AO,
Art. 17 Abs. 3 lit. b DSGVO). Auf ein Loeschverlangen hin werden
deshalb Name, Mail, Telefon und Adresse geleert und die Zeile bleibt
stehen. Diese Spalte haelt fest, dass und wann das passiert ist —
sonst waere von aussen nicht unterscheidbar, ob ein Datensatz
anonymisiert wurde oder nur unvollstaendig ist.
"""
from alembic import op
import sqlalchemy as sa


revision = "h9k3m6p2r5t8"
down_revision = "g8h2k4m7n1p5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "kunden",
        sa.Column("anonymized_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("kunden", "anonymized_at")
