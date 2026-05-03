"""
RechnungPosition: Einzelne Position auf einer Rechnung.

Mehrere Positionen pro Rechnung (Möbelmontage 250€ + Anfahrt 50€ + Material 70€).
Source-of-Truth fuer den Lexware-Voucher; Audit-Trail in unserer DB.
"""
import datetime as dt
import decimal
import uuid

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column


from core.database.base import Base


class RechnungPosition(Base):
    """Eine Zeile auf einer Rechnung (Leistung/Produkt)."""

    __tablename__ = "rechnung_positionen"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )

    rechnung_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("rechnungen.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Reihenfolge der Position auf der Rechnung (1, 2, 3, ...)
    position_nr: Mapped[int] = mapped_column(Integer, nullable=False)

    # Inhalt
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    beschreibung: Mapped[str | None] = mapped_column(Text, nullable=True)
    menge: Mapped[decimal.Decimal] = mapped_column(
        Numeric(precision=12, scale=3), nullable=False, server_default="1"
    )
    einheit: Mapped[str] = mapped_column(
        String(50), nullable=False, server_default="Stueck"
    )
    preis_brutto_eur: Mapped[decimal.Decimal] = mapped_column(
        Numeric(precision=10, scale=2), nullable=False
    )
    mwst_prozent: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="19"
    )

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    @property
    def gesamt_brutto(self) -> decimal.Decimal:
        """Position-Gesamtpreis = Menge * Einzelpreis."""
        return (self.menge or decimal.Decimal("0")) * (self.preis_brutto_eur or decimal.Decimal("0"))

    def __repr__(self) -> str:
        return (
            f"<RechnungPosition #{self.position_nr} "
            f"{self.name!r} {self.menge}x{self.preis_brutto_eur}>"
        )
