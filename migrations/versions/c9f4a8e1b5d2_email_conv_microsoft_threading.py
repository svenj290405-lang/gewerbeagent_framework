"""email_conversations.microsoft_conversation_id fuer Threading

Microsoft Graph haengt jedem Mail-Thread eine conversationId an. Wir
speichern sie zusaetzlich zum bestehenden last_message_id (das den
Outbound-Message-ID-Wert fuer In-Reply-To-Matching haelt).

Use-Case: thread-weites Grouping wenn der Kunde mehrere Replys
schreibt und wir spaeter alle Q-Antworten zu einem Thread anzeigen
wollen. Aktuell wird die Spalte gelesen aber NICHT als primaerer
Matching-Key benutzt — In-Reply-To via last_message_id bleibt der
schnellste Pfad (1:1-Match auf der zuletzt versendeten Q-Mail).

Index: weil das Feld als sekundaerer Lookup-Key fuer "alle Convs
dieses Threads" dient.

Revision ID: c9f4a8e1b5d2
Revises: 10be0f9bdb1a
Create Date: 2026-05-18 21:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c9f4a8e1b5d2"
down_revision: Union[str, Sequence[str], None] = "10be0f9bdb1a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "email_conversations",
        sa.Column(
            "microsoft_conversation_id",
            sa.String(length=255),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_email_conv_ms_conv_id",
        "email_conversations",
        ["microsoft_conversation_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_email_conv_ms_conv_id", table_name="email_conversations")
    op.drop_column("email_conversations", "microsoft_conversation_id")
