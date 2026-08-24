"""email_conversations: Zeitpunkt der automatischen Mail-Buchung

Revision ID: n6q9t2u5x8z3
Revises: m5p8s1t4w7y2
Create Date: 2026-08-24 16:20:00.000000

Eine Kundenmail kann bei uns ohne Rueckfrage einen Kalendereintrag
erzeugen. Die Gates dafuer sind eng (Name + Telefon Pflicht, kein
zweiter Termin, Spam-Throttle), es gab aber keinen Deckel auf die
MENGE: wer sich zehn Wegwerf-Adressen nimmt, hat den Kalender voll.

`booked_at` ist der Zaehler dafuer — eine Zeile pro tatsaechlich
automatisch gebuchtem Termin, mit Zeitstempel. Bewusst NICHT ueber
updated_at gezaehlt (das wandert bei jeder Folge-Mail) und nicht ueber
app_usage_events (das misst, wie aktiv ein BETRIEB seine App nutzt —
Fremd-Mails wuerden die Kundenampel gruen faerben).

Index auf (tenant_id, booked_at) partial: nur gebuchte Zeilen sind
gemeint, das haelt ihn klein.
"""
from alembic import op
import sqlalchemy as sa


revision = "n6q9t2u5x8z3"
down_revision = "m5p8s1t4w7y2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("email_conversations", sa.Column(
        "booked_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "ix_email_conv_booked_at", "email_conversations",
        ["tenant_id", "booked_at"],
        postgresql_where=sa.text("booked_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_email_conv_booked_at", table_name="email_conversations")
    op.drop_column("email_conversations", "booked_at")
