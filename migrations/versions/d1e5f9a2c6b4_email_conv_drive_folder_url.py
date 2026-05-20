"""email_conversations.drive_folder_url

Speichert den Link zum Kunden-Drive-Ordner an der Mail-Konversation,
sobald der Kunde das Anfrage-Formular ausgefuellt hat (Drive-Archiv).
Beim spaeteren Termin-Buchen wird der Link in die Kalender-Event-
Beschreibung geschrieben, damit der Handwerker direkt zu den
Anfrage-Daten + Fotos springen kann.

Nullable, kein Index — wird nur ueber die Konversations-ID (PK)
gelesen, kein Lookup-Key.

Revision ID: d1e5f9a2c6b4
Revises: c9f4a8e1b5d2
Create Date: 2026-05-19 21:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d1e5f9a2c6b4"
down_revision: Union[str, Sequence[str], None] = "c9f4a8e1b5d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "email_conversations",
        sa.Column(
            "drive_folder_url",
            sa.String(length=1000),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("email_conversations", "drive_folder_url")
