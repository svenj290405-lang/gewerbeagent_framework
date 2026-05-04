"""TenantLeistung - Wissensbasis-Eintrag fuer eine angebotene Leistung.

Pro Tenant gibt es N Leistungen mit Preis + Einheit + Standardbeschreibung.
Gemini nutzt diese Tabelle beim Angebots-Erstellen, um:
  1. Preise zu matchen (Tenant sagt "Moebelmontage 4 Stunden" -> 75 EUR/Std aus DB)
  2. Standardbeschreibungen zu nutzen (statt jedes Mal neu zu generieren)
  3. Aliase zu erkennen (Anrufer sagt "Schrank aufbauen" -> matched "Moebelmontage")
"""
from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base


class TenantLeistung(Base):
    __tablename__ = "tenant_leistungen"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Identifikation
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    aliase: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)

    # Preis + Einheit
    preis_eur: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    einheit: Mapped[str] = mapped_column(String(50), nullable=False)
    mwst_prozent: Mapped[int] = mapped_column(Integer, default=19, nullable=False)

    # Texte
    standard_beschreibung: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Workflow
    aktiv: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sortierung: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # created_at + updated_at kommen aus Base

    def __repr__(self) -> str:
        return (
            f"<TenantLeistung id={self.id} name={self.name!r} "
            f"preis={self.preis_eur} {self.einheit}>"
        )
