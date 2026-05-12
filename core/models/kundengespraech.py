"""Kundengespraech - Aufgezeichnetes Gespraech zwischen Tenant und Kunde.

Workflow:
1. Tenant nimmt Gespraech via Telegram-/aufnahme auf
2. Gemini analysiert: Transkript + Briefing + Positionen + Termin
3. Wird hier gespeichert
4. Optional: Lexware-Angebot draus erstellt (angebot_id verknuepft)
5. Briefing-Befehle lesen aus dieser Tabelle
"""
from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database.base import Base


class Kundengespraech(Base):
    __tablename__ = "kundengespraeche"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Kundendaten
    kunde_name: Mapped[str] = mapped_column(String(300), nullable=False)
    kunde_kontakt_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    # Audio-Metadaten
    gespraech_datum: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    audio_dauer_sekunden: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_transcript: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Strukturierte Daten
    briefing_kurz: Mapped[str | None] = mapped_column(Text, nullable=True)
    notizen_lang: Mapped[str | None] = mapped_column(Text, nullable=True)
    todos: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)

    # Termin
    termin_datum: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    termin_ort: Mapped[str | None] = mapped_column(String(300), nullable=True)

    # Verknuepfung zum Angebot
    angebot_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("angebote.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Workflow + Qualitaet
    confidence: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(
        String(50), default="erfasst", nullable=False
    )

    # Phase-4-Multi-Mitarbeiter:
    # - assigned_employee_id: wer fuehrt das Gespraech durch / kuemmert
    #   sich um den daraus entstehenden Termin (gesetzt vom Skill-Router
    #   Phase 5, oder manuell beim /aufnahme-Wizard)
    # - created_by_employee_id: wer hat die Aufnahme angelegt
    # Beide UUID NULL (Backfill auf Default-Employee, neue Eintraege
    # explizit gesetzt). FK SET NULL damit deaktivierte Mitarbeiter
    # die Historie nicht killen.
    assigned_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    created_by_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )

    # Phase-6-Reschedule-Mail-Tracking: wenn ein Kundengespraech-Termin
    # wegen Krankheit auf einen Kollegen umgebucht wird, schicken wir
    # dem Kunden eine "neuer Termin/anderer Mitarbeiter"-Mail. Die
    # message_id + conversation_id merken wir hier um (a) doppelten
    # Versand zu vermeiden und (b) Replies im Mail-Intake-Sticky-
    # Routing auf den neuen Mitarbeiter zuzuordnen.
    reschedule_mail_message_id: Mapped[str | None] = mapped_column(
        String(500), nullable=True,
    )
    reschedule_mail_conversation_id: Mapped[str | None] = mapped_column(
        String(500), nullable=True,
    )

    # created_at + updated_at aus Base

    def __repr__(self) -> str:
        return (
            f"<Kundengespraech id={self.id} kunde={self.kunde_name!r} "
            f"datum={self.gespraech_datum} status={self.status}>"
        )
