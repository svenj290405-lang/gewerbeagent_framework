"""VoiceCall - Eingehendes Telefonat ueber den ElevenLabs-Voice-Agent.

Workflow:
1. Kunde ruft die Tenant-Nummer an, ElevenLabs nimmt das Gespraech an
2. Nach Gespraechsende feuert ElevenLabs den /call_ended-Webhook
   (plugins/voice_init/handler.py::_handle_call_ended)
3. Dort wird pro Anruf EINE Zeile hier gespeichert
4. Der Tenant sieht die seit dem letzten Mal neuen Anrufe via /anrufe

Bewusst getrennt von Kundengespraech (= via /aufnahme aufgenommene
Vor-Ort-Gespraeche). Das hier sind die echten eingehenden Telefonate.
"""
from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base

# Anruf-Ergebnis (vom Webhook geliefert, ElevenLabs-Konvention)
CALL_OUTCOME_COMPLETED = "completed"
CALL_OUTCOME_INCOMPLETE = "incomplete"
CALL_OUTCOME_NO_AUDIO = "no_audio"


class VoiceCall(Base):
    """Ein eingehendes Telefonat ueber den Voice-Agent."""

    __tablename__ = "voice_calls"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Telefonie-Metadaten (alle optional — ElevenLabs liefert je nach
    # Provider/Outcome unterschiedlich viel)
    caller_number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    called_number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(30), nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # Optional vom Agent waehrend des Gespraechs erfasst (falls geliefert)
    kunde_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    anliegen: Mapped[str | None] = mapped_column(String(500), nullable=True)
    zusammenfassung: Mapped[str | None] = mapped_column(Text, nullable=True)

    # created_at (aus Base) = Anruf-Zeitpunkt; updated_at aus Base

    __table_args__ = (
        # /anrufe-Liste: Tenant-Anrufe neueste zuerst, gefiltert auf
        # "seit letztem Aufruf" (created_at > last_seen).
        Index("ix_voice_calls_tenant_created", "tenant_id", "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<VoiceCall id={self.id} tenant={self.tenant_id} "
            f"caller={self.caller_number!r} dauer={self.duration_seconds}s "
            f"outcome={self.outcome}>"
        )
