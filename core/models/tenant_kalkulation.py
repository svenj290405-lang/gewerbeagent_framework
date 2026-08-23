"""TenantKalkulation = eine Ueberschlags-Formel des Betriebs.

Die Tabelle ``tenant_kalkulationen`` existiert seit Migration r4m8h3k6n2p7
(2026-05-10), hatte aber nie ein Modell und nie eine Zeile Code — sie war
fuer einen Telegram-Wizard gedacht, der nie gebaut wurde und dessen Kanal
inzwischen abgeschaltet ist. Hier wird sie nachgezogen, weil sie die
haeufigste Telefonfrage beantwortbar macht: "Was kostet das ungefaehr?"

Prinzip (Hybrid, bewusst nicht "die KI rechnet"):
  1. Der Betrieb hinterlegt eine Formel, z.B. ``qm * preis_qm + anfahrt``
  2. Q (Voice/Mail/App) sammelt nur die Variablen-Werte beim Kunden ein
  3. Gerechnet wird deterministisch in Python (core/services/kalkulation.py)

Damit kann ein Sprachmodell den Preis nicht halluzinieren — es fuellt
Zahlen in eine Formel, die der Betrieb selbst aufgeschrieben hat. Das
Ergebnis ist immer ein *Richtwert*, nie ein Angebot; die Formulierung
dazu steckt im Prompt bzw. in ``ueberschlag_text()``.
"""
from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Integer, String, Text, func,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base


KALKULATION_SOURCE_MANUAL = "manual"    # in der App angelegt
KALKULATION_SOURCE_Q = "q"              # Q hat sie auf Zuruf angelegt
KALKULATION_SOURCE_TEMPLATE = "template"  # Branchen-Vorlage


class TenantKalkulation(Base):
    """Eine benannte Formel mit Variablen, pro Tenant."""

    __tablename__ = "tenant_kalkulationen"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Freie Einordnung ("sanitaer", "maler", …) — nur zum Gruppieren.
    kategorie: Mapped[str] = mapped_column(String(50), nullable=False, index=True)

    # Was der Kunde fragt: "Bad fliesen", "Wand streichen"
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    # Ausdruck ueber den Variablen, z.B. "qm * 45 + anfahrt"
    formel: Mapped[str] = mapped_column(String(1000), nullable=False)

    # Namen der Variablen, die Q beim Kunden erfragen muss
    variablen: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, default=list, server_default="{}"
    )

    einheit: Mapped[str | None] = mapped_column(String(50), nullable=True)
    beschreibung: Mapped[str | None] = mapped_column(Text, nullable=True)

    aktiv: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    sortierung: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    source: Mapped[str] = mapped_column(
        String(20), nullable=False, default=KALKULATION_SOURCE_MANUAL,
        server_default=KALKULATION_SOURCE_MANUAL,
    )
    # Aus der Ur-Migration; kein Excel-Import gebaut, Spalte bleibt leer.
    excel_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<TenantKalkulation {self.name!r} = {self.formel!r}>"
