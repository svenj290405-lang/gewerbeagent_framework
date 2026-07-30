"""Auftragsprozess — tenantweit konfigurierbare Zwischenschritte.

Der Auftrags-Lifecycle (``AUFTRAG_LIFECYCLE`` in angebot.py) hat fuenf
KERN-Schritte, an denen echte Logik haengt: ``arbeit_laeuft`` traegt den
Fortschritts-Regler, ``arbeit_fertig`` fuehrt in den Rechnungs-Flow und
``rechnung_gesendet`` wird ausschliesslich von ``finalize_and_send_invoice``
gesetzt (Lexware finalisieren + Rechnungs-Mail = Geld-Pfad). Diese fuenf
sind darum unveraenderlich: nicht loeschbar, nicht umsortierbar.

Was der Handwerker frei gestalten darf, sind die Schritte DAZWISCHEN
("Aufmass nehmen", "Material bestellen", "Abnahme mit Kunde"). Genau die
liegen in dieser Tabelle.

Verankerung statt globaler Sortierung: ein eigener Schritt speichert NICHT
seine absolute Position, sondern den Kern-Schritt, hinter dem er steht
(``nach_kern_status``; NULL = ganz am Anfang, vor dem ersten Kern-Schritt)
plus ``sort_index`` innerhalb dieser Luecke. Damit ist die Reihenfolge der
Kern-Schritte strukturell nicht kaputtzumachen — auch nicht durch einen
fehlerhaften Client, der eine beliebige Liste hochschickt. Drag & Drop im
Editor wird zu "(nach_kern_status, sort_index) neu setzen".

Pro Auftrag wird das Abhaken eines eigenen Schritts in
``AuftragSchrittStatus`` festgehalten. Kern-Schritte brauchen dort nichts:
ihr Erledigt-Zustand ergibt sich aus ``angebote.status``.
"""
from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    DateTime, ForeignKey, Index, Integer, String, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base


# Obergrenze fuer eigene Schritte pro Tenant — verhindert, dass ein
# durchgedrehter Client die Prozess-Definition aufblaeht (das Diagramm
# wird ohnehin ab ~20 Boxen unbenutzbar).
MAX_EIGENE_SCHRITTE = 40
# Maximale Label-Laenge im Editor (die Box im Diagramm bleibt lesbar).
MAX_SCHRITT_LABEL = 60


class AuftragProzessSchritt(Base):
    """Ein eigener (tenant-definierter) Zwischenschritt im Auftragsprozess."""

    __tablename__ = "auftrag_prozess_schritte"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    label: Mapped[str] = mapped_column(String(MAX_SCHRITT_LABEL), nullable=False)

    # Anker: Kern-Status, HINTER dem dieser Schritt steht.
    # NULL = vor dem ersten Kern-Schritt.
    nach_kern_status: Mapped[str | None] = mapped_column(
        String(50), nullable=True,
    )
    # Reihenfolge innerhalb der Luecke hinter ``nach_kern_status``.
    sort_index: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )

    # created_at + updated_at aus Base

    __table_args__ = (
        Index(
            "ix_auftrag_prozess_schritte_order",
            "tenant_id", "nach_kern_status", "sort_index",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<AuftragProzessSchritt id={self.id} label={self.label!r} "
            f"nach={self.nach_kern_status} idx={self.sort_index}>"
        )


class AuftragSchrittStatus(Base):
    """Abgehakter eigener Schritt fuer EINEN konkreten Auftrag (Angebot).

    Existiert die Zeile, gilt der Schritt als erledigt — Abhaken legt an,
    Haken-entfernen loescht. Kein Bool-Feld, damit "nie angefasst" und
    "bewusst zurueckgesetzt" nicht auseinanderfallen koennen.
    """

    __tablename__ = "auftrag_schritt_status"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    angebot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("angebote.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # Wird der Schritt aus der Prozess-Definition geloescht, verschwinden
    # auch die Haken — der Schritt existiert dann ja nicht mehr.
    schritt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("auftrag_prozess_schritte.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    erledigt_am: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    erledigt_von_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="SET NULL"),
        nullable=True,
    )

    # created_at + updated_at aus Base

    __table_args__ = (
        UniqueConstraint(
            "angebot_id", "schritt_id", name="uq_auftrag_schritt_status",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<AuftragSchrittStatus angebot={self.angebot_id} "
            f"schritt={self.schritt_id}>"
        )


__all__ = [
    "AuftragProzessSchritt",
    "AuftragSchrittStatus",
    "MAX_EIGENE_SCHRITTE",
    "MAX_SCHRITT_LABEL",
]
