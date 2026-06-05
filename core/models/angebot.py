"""Angebot - Header eines Angebots.

Pro Tenant gibt es N Angebote. Jedes Angebot hat 1-M angebot_positionen.
Wird via Telegram-Bot (Voice/Text) erstellt, in Lexware als Quotation gespeichert.
"""
from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database.base import Base


# Beta-1 B1-6: Status-Konstanten als Modul-Exports (frueher nur im
# Doc-String). Damit gibts grep-baren Code statt magic strings.
ANGEBOT_STATUS_ERSTELLT = "erstellt"
ANGEBOT_STATUS_IN_LEXWARE = "in_lexware"
ANGEBOT_STATUS_MAIL_QUEUED = "mail_queued"     # NEU: Microsoft Graph down → Queue
ANGEBOT_STATUS_MAIL_SENT = "mail_sent"
ANGEBOT_STATUS_MAIL_FAILED = "mail_failed"     # NEU: 3 Retries durch, dead
ANGEBOT_STATUS_ACCEPTED = "accepted"
ANGEBOT_STATUS_REJECTED = "rejected"
ANGEBOT_STATUS_RECHNUNG_ERSTELLT = "rechnung_erstellt"
# Lifecycle nach Angebots-Versand (manuelle Schritte des Handwerkers
# ueber /auftraege). work_done loest den Rechnungs-Versand aus.
ANGEBOT_STATUS_WORK_IN_PROGRESS = "arbeit_laeuft"
ANGEBOT_STATUS_WORK_DONE = "arbeit_fertig"
ANGEBOT_STATUS_RECHNUNG_GESENDET = "rechnung_gesendet"
ANGEBOT_STATUS_ABGEBROCHEN = "abgebrochen"

# Auftrags-Lifecycle in lesbarer Reihenfolge — Reihenfolge der Schritte
# in der /auftraege-UI. Status >= rechnung_erstellt zaehlt als
# "laufender Auftrag".
AUFTRAG_LIFECYCLE = [
    ANGEBOT_STATUS_RECHNUNG_ERSTELLT,    # Angebot in Lexware, Rechnung als Draft bereit
    ANGEBOT_STATUS_ACCEPTED,             # Kunde hat zugesagt (manuell oder per Mail-Reply)
    ANGEBOT_STATUS_WORK_IN_PROGRESS,     # Handwerker arbeitet
    ANGEBOT_STATUS_WORK_DONE,            # Handwerker fertig → Rechnung wird versendet
    ANGEBOT_STATUS_RECHNUNG_GESENDET,    # Rechnung beim Kunden
]
AUFTRAG_LIFECYCLE_LABELS = {
    ANGEBOT_STATUS_RECHNUNG_ERSTELLT: "📋 Angebot raus",
    ANGEBOT_STATUS_ACCEPTED: "✅ Angenommen",
    ANGEBOT_STATUS_WORK_IN_PROGRESS: "🔨 Arbeit laeuft",
    ANGEBOT_STATUS_WORK_DONE: "🏁 Fertig",
    ANGEBOT_STATUS_RECHNUNG_GESENDET: "📨 Rechnung raus",
    ANGEBOT_STATUS_ABGEBROCHEN: "❌ Abgebrochen",
}


class Angebot(Base):
    __tablename__ = "angebote"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Quelle
    quelle: Mapped[str] = mapped_column(String(20), nullable=False)
    raw_input: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Kundendaten
    kunde_name: Mapped[str] = mapped_column(String(300), nullable=False)
    kunde_strasse: Mapped[str | None] = mapped_column(String(300), nullable=True)
    kunde_plz: Mapped[str | None] = mapped_column(String(20), nullable=True)
    kunde_ort: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # Gesamtbetrag (errechnet aus Positionen, gespeichert fuer Stats)
    gesamtbetrag_brutto_eur: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 2), nullable=True
    )

    # Lexware-Anbindung
    lexware_quotation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    lexware_voucher_number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    lexware_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Lexware-Invoice (Rechnungs-Draft, wird in der /angebot-Pipeline
    # mit-angelegt und spaeter beim /auftraege-Schritt "fertig"
    # finalisiert + per Mail an den Kunden geschickt).
    lexware_invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    # AI-generierte Texte
    introduction_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    remark_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Workflow
    # Status-Werte:
    #   erstellt | in_lexware | mail_sent | accepted | rejected | rechnung_erstellt
    status: Mapped[str] = mapped_column(String(50), default="erstellt", nullable=False)
    confidence: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Mail-Versand & Tracking (fuer Auto-Rechnung-Erkennung)
    kunde_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mail_sent_to: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mail_sent_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Microsoft-Graph-IDs der versandten Mail - zum Match bei Antworten
    mail_message_id: Mapped[str | None] = mapped_column(String(500), nullable=True)
    mail_internet_message_id: Mapped[str | None] = mapped_column(
        String(500), nullable=True, index=True,
    )
    mail_conversation_id: Mapped[str | None] = mapped_column(
        String(500), nullable=True, index=True,
    )

    # Annahme/Ablehnung durch den Kunden (per Mail-Reply)
    accepted_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rejected_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Falls aus dem Angebot eine Rechnung gebaut wurde
    rechnung_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    # Arbeits-Fortschritt 0-100 % (Regler im "Aktuelles"-Tab fuer laufende
    # Auftraege im Status arbeit_laeuft). Bei 100 % wird der Auftrag
    # fertiggemeldet und der Handwerker in Q zur Rechnung gefuehrt.
    arbeit_fortschritt: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )

    # Erstellung (fuer Listings/Sortierung)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    positionen: Mapped[list[AngebotPosition]] = relationship(  # noqa: F821
        "AngebotPosition",
        back_populates="angebot",
        cascade="all, delete-orphan",
        order_by="AngebotPosition.position_nr",
    )

    def __repr__(self) -> str:
        return (
            f"<Angebot id={self.id} kunde={self.kunde_name!r} "
            f"gesamt={self.gesamtbetrag_brutto_eur} status={self.status}>"
        )
