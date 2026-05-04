"""AngebotPosition - Einzelne Position eines Angebots.

Optional verknuepft zu tenant_leistungen.id, falls aus Wissensbasis-Vorlage.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database.base import Base


class AngebotPosition(Base):
    __tablename__ = "angebot_positionen"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    angebot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("angebote.id", ondelete="CASCADE"),
        nullable=False,
    )

    position_nr: Mapped[int] = mapped_column(Integer, nullable=False)

    # Position-Daten
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    beschreibung: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Mengen + Preise
    menge: Mapped[Decimal] = mapped_column(
        Numeric(12, 3), default=Decimal("1"), nullable=False
    )
    einheit: Mapped[str] = mapped_column(String(50), default="Stueck", nullable=False)
    preis_brutto_eur: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    mwst_prozent: Mapped[int] = mapped_column(Integer, default=19, nullable=False)

    # Verknuepfung zur Wissensbasis
    leistung_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenant_leistungen.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Relationships
    angebot: Mapped["Angebot"] = relationship(  # noqa: F821
        "Angebot", back_populates="positionen"
    )

    @property
    def gesamt_brutto(self) -> Decimal:
        """Gesamtbetrag dieser Position (menge x preis_brutto_eur)."""
        return self.menge * self.preis_brutto_eur

    def __repr__(self) -> str:
        return (
            f"<AngebotPosition id={self.id} nr={self.position_nr} "
            f"name={self.name!r} {self.menge}x{self.preis_brutto_eur}>"
        )
