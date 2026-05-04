"""Angebot - Header eines Angebots.

Pro Tenant gibt es N Angebote. Jedes Angebot hat 1-M angebot_positionen.
Wird via Telegram-Bot (Voice/Text) erstellt, in Lexware als Quotation gespeichert.
"""
from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database.base import Base


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

    # AI-generierte Texte
    introduction_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    remark_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Workflow
    status: Mapped[str] = mapped_column(String(50), default="erstellt", nullable=False)
    confidence: Mapped[str | None] = mapped_column(String(20), nullable=True)

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
