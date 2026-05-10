"""
TenantKalkulation = mathematische Berechnungs-Formeln pro Tenant.

Schwester-Konzept zu TenantKnowledge: der Handwerker pflegt via Telegram
oder Excel-Upload Kalkulationsregeln, die Gemini bei der Angebots-
Erstellung beachten muss. Beispiele:

  Anfahrtspauschale:    entfernung_km * 0.50
  Material-Aufschlag:   einkaufspreis * 1.30
  Notfall-Zuschlag:     stunden * 75 + 50

Hybrid-Berechnung:
  - Gemini liest die Formel als Kontext und extrahiert beim Angebot
    nur die *Variablen-Werte* (z.B. entfernung_km=42, stunden=3).
  - Der Aufnahme-Handler ruft compute_kalkulation() auf und ersetzt den
    extrahierten Preis durch das deterministische Ergebnis.
  - Damit bleibt der Preis 100% reproduzierbar - die LLM bewertet nur
    Sprache, das Geld rechnet Python.

Quellen (source):
  - "manual"  ... per /kalkulation Wizard eingegeben
  - "excel"   ... aus xlsx-Upload extrahiert (Formel als Text)
"""
import datetime as dt
import uuid

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database.base import Base


# Kategorien fuer Kalkulationen (eigene Liste, nicht zu verwechseln mit
# TenantKnowledge-Kategorien). Bewusst flach + uebersichtlich.
KALK_KATEGORIE_ANFAHRT = "anfahrt"
KALK_KATEGORIE_MATERIAL = "material"
KALK_KATEGORIE_STUNDENLOHN = "stundenlohn"
KALK_KATEGORIE_AUFSCHLAG = "aufschlag"
KALK_KATEGORIE_RABATT = "rabatt"
KALK_KATEGORIE_PAUSCHALE = "pauschale"
KALK_KATEGORIE_SONSTIGES = "sonstiges"

ALLE_KALK_KATEGORIEN = (
    KALK_KATEGORIE_ANFAHRT,
    KALK_KATEGORIE_MATERIAL,
    KALK_KATEGORIE_STUNDENLOHN,
    KALK_KATEGORIE_AUFSCHLAG,
    KALK_KATEGORIE_RABATT,
    KALK_KATEGORIE_PAUSCHALE,
    KALK_KATEGORIE_SONSTIGES,
)

KALK_KATEGORIE_LABELS = {
    KALK_KATEGORIE_ANFAHRT: "Anfahrt",
    KALK_KATEGORIE_MATERIAL: "Material",
    KALK_KATEGORIE_STUNDENLOHN: "Stundenlohn",
    KALK_KATEGORIE_AUFSCHLAG: "Aufschlag / Zuschlag",
    KALK_KATEGORIE_RABATT: "Rabatt",
    KALK_KATEGORIE_PAUSCHALE: "Pauschale",
    KALK_KATEGORIE_SONSTIGES: "Sonstiges",
}

# Quellen
KALK_SOURCE_MANUAL = "manual"
KALK_SOURCE_EXCEL = "excel"


class TenantKalkulation(Base):
    """Eine Kalkulationsregel eines Tenants."""

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

    # Eine der ALLE_KALK_KATEGORIEN-Konstanten
    kategorie: Mapped[str] = mapped_column(String(50), nullable=False, index=True)

    # Kurztitel, z.B. "Anfahrtspauschale" oder "Notfall-Zuschlag"
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    # Die eigentliche Formel als Text. Erlaubt: Zahlen, +-*/(), **, Variablen
    # (snake_case), und die Funktionen: min, max, round, abs, ceil, floor.
    # Beispiele:
    #   entfernung_km * 0.50
    #   max(50, stunden * 75)
    #   einkaufspreis * 1.30 + 5
    formel: Mapped[str] = mapped_column(String(1000), nullable=False)

    # Aus formel extrahierte Variablennamen (Cache; wird beim Speichern
    # gefuellt). Gemini muss diese in der Extraktion mitliefern.
    variablen: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, server_default="{}"
    )

    # Was kommt am Ende raus? (EUR, EUR/Stunde, %, ...). Reines Doku-Feld;
    # primaer fuer den Handwerker im /kalkulation_anzeigen-Output.
    einheit: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Optional: Klartext-Beschreibung fuer Gemini-Kontext.
    beschreibung: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Soft-Delete-Flag, damit Excel-Re-Imports altes nicht hard-loeschen
    aktiv: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )

    # Sortierung in /kalkulation_anzeigen
    sortierung: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )

    # "manual" | "excel"
    source: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=KALK_SOURCE_MANUAL
    )

    # Bei source="excel": Original-Dateiname (Doku, kein Bytes-Speicher)
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

    tenant: Mapped["Tenant"] = relationship()  # noqa: F821

    def __repr__(self) -> str:
        return (
            f"<TenantKalkulation {self.kategorie}/{self.name} "
            f"tenant={self.tenant_id} formel={self.formel!r}>"
        )
