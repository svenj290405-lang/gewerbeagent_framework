"""email_conversations.genutztes_wissen: was Q in der Antwort verwendet hat

Revision ID: f7y4a1n6q3t8
Revises: e6x3z9m5p2s7
Create Date: 2026-08-23 12:00:00.000000

Antwort-Nachweis: Bisher stand in der Anfragen-Ansicht nur, WAS Q dem
Kunden geantwortet hat — nicht, WORAUF er sich dabei gestuetzt hat. Damit
war weder pruefbar, ob eine Auskunft gedeckt war, noch nachvollziehbar,
warum Q etwas Falsches gesagt hat (steht der Fehler in der Wissensbasis,
oder hat das Modell danebengegriffen?).

Q gibt die genutzten Angaben jetzt selbst mit zurueck (Feld
``genutztes_wissen`` im Dialog-Schema) und sie werden hier zur
Konversation gespeichert. JSONB, weil es eine kurze Liste von
Textschnipseln ist und wir nie danach filtern.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "f7y4a1n6q3t8"
down_revision: Union[str, Sequence[str], None] = "e6x3z9m5p2s7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "email_conversations",
        sa.Column("genutztes_wissen", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("email_conversations", "genutztes_wissen")
