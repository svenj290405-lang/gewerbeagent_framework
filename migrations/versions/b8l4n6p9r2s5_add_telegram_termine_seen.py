"""telegram_termine_seen: gesehene Event-IDs pro Telegram-Chat

Revision ID: b8l4n6p9r2s5
Revises: a9k2m4n6p8q1
Create Date: 2026-05-17 19:30:00.000000

/neue_termine zeigt nur Kalender-Events deren event_id beim letzten
Aufruf NICHT in der Liste war. Damit das funktioniert speichern wir
pro Telegram-Chat eine Liste der zuletzt gesehenen event_ids und
vergleichen beim naechsten Aufruf gegen die aktuelle Termin-Liste.

Eigene Tabelle statt TelegramState-Reuse: TelegramState ist single-
slot pro chat_id und steuert aktive Wizards — wir wollen keine
Wizard-Konflikte. Wir sind hier ein simples key-value-store fuer
einen Read-Modify-Write-Pfad.

Schema:
- chat_id BIGINT PK (1 Eintrag pro Chat)
- event_ids JSONB (Liste der zuletzt gesehenen event_id-Strings)
- updated_at TIMESTAMPTZ (Audit, falls jemand Stale-Eintraege cleanen will)
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b8l4n6p9r2s5"
down_revision: Union[str, None] = "a9k2m4n6p8q1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "telegram_termine_seen",
        sa.Column("chat_id", sa.BigInteger, primary_key=True),
        sa.Column(
            "event_ids", postgresql.JSONB,
            nullable=False, server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("telegram_termine_seen")
