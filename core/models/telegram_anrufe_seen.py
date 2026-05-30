"""Tracking-Tabelle fuer /anrufe.

Pro Telegram-Chat merken wir den Zeitpunkt des letzten /anrufe-Aufrufs.
Beim naechsten Aufruf zeigen wir nur Anrufe deren created_at NACH diesem
Zeitpunkt liegt — also wirklich neu eingegangene Telefonate.

Bewusst timestamp-basiert (nicht id-Liste wie TelegramTermineSeen): Anrufe
kommen streng chronologisch rein, ein einzelner Wasserstand reicht.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import BigInteger, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base


class TelegramAnrufeSeen(Base):
    """Letzter /anrufe-Aufruf-Zeitpunkt pro Telegram-Chat."""

    __tablename__ = "telegram_anrufe_seen"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Wasserstand: Anrufe mit created_at > last_seen_at gelten als neu.
    last_seen_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<TelegramAnrufeSeen chat={self.chat_id} "
            f"last_seen={self.last_seen_at}>"
        )
