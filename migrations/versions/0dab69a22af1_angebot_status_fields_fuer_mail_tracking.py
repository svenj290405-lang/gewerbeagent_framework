"""angebot status fields fuer mail tracking

Revision ID: 0dab69a22af1
Revises: e636dec91e7a
Create Date: 2026-05-09 21:19:47.404152

Erweitert die angebote-Tabelle um Mail-Versand- und Tracking-Felder, damit
ein versandtes Angebot bei einer Reply-Mail wieder gefunden werden kann
(via Microsoft conversationId / internetMessageId) und der Status sauber
dokumentiert wird (mail_sent / accepted / rejected / rechnung_erstellt).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0dab69a22af1"
down_revision: Union[str, Sequence[str], None] = "e636dec91e7a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("angebote", sa.Column("kunde_email", sa.String(length=255), nullable=True))
    op.add_column("angebote", sa.Column("mail_sent_to", sa.String(length=255), nullable=True))
    op.add_column("angebote", sa.Column("mail_sent_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("angebote", sa.Column("mail_message_id", sa.String(length=500), nullable=True))
    op.add_column(
        "angebote", sa.Column("mail_internet_message_id", sa.String(length=500), nullable=True)
    )
    op.add_column(
        "angebote", sa.Column("mail_conversation_id", sa.String(length=500), nullable=True)
    )
    op.add_column("angebote", sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("angebote", sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("angebote", sa.Column("rechnung_id", sa.UUID(), nullable=True))

    op.create_index(
        "ix_angebote_mail_conversation_id",
        "angebote",
        ["mail_conversation_id"],
        unique=False,
    )
    op.create_index(
        "ix_angebote_mail_internet_message_id",
        "angebote",
        ["mail_internet_message_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_angebote_mail_internet_message_id", table_name="angebote")
    op.drop_index("ix_angebote_mail_conversation_id", table_name="angebote")
    op.drop_column("angebote", "rechnung_id")
    op.drop_column("angebote", "rejected_at")
    op.drop_column("angebote", "accepted_at")
    op.drop_column("angebote", "mail_conversation_id")
    op.drop_column("angebote", "mail_internet_message_id")
    op.drop_column("angebote", "mail_message_id")
    op.drop_column("angebote", "mail_sent_at")
    op.drop_column("angebote", "mail_sent_to")
    op.drop_column("angebote", "kunde_email")
