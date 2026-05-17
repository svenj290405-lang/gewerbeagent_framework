"""Tracking-Tabelle fuer /neue_termine.

Pro Telegram-Chat speichern wir die Liste der event_ids, die beim
letzten /neue_termine-Aufruf in der Termin-Liste waren. Beim naechsten
Aufruf zeigen wir nur Events deren event_id NICHT in dieser Liste
steckt — also wirklich neu hinzugekommen sind.

Trennung von TelegramState (= Wizard-State) bewusst: das hier ist
ein simples key-value-store mit Read-Modify-Write-Semantik, kein
Wizard. Konflikte mit aktiven Wizards waeren peinlich.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import BigInteger, DateTime, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base


class TelegramTermineSeen(Base):
    """Letzte gesehene Kalender-Event-IDs pro Telegram-Chat."""

    __tablename__ = "telegram_termine_seen"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Liste von event_id-Strings (Google: lang-base64; Outlook: AAMk…).
    # Wir speichern als JSONB damit der Caller einfach list(...) draus
    # macht ohne JSON-Parsing zu wrappen.
    event_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]",
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<TelegramTermineSeen chat={self.chat_id} "
            f"n_event_ids={len(self.event_ids or [])}>"
        )
