"""Kunde — echte Kundendatenbank statt Namensstring.

Ein Kunde ist eine Person oder Firma, die pro Tenant genau einmal
existiert. Die technische `id` IST die interne Kundennummer — es gibt
keine separate K-Nummer. Bestandstabellen (Angebote, Rechnungen,
Gespraeche, ...) verweisen ueber ihr nullable `kunde_id` hierher; die
alten Namensfelder bleiben parallel befuellt, bis die Umstellung
stabil laeuft (siehe Kundendatenbank_Umsetzungsplan.md).

Identitaet: `identity_key` ("email:..." > "tel:..." > Namens-Slug,
gebildet wie beim Drive-Ordner) ist der Anlage-Schluessel und bleibt
nach der Anlage unveraendert. Der Lookup laeuft NICHT nur ueber diesen
Key, sondern ueber die Kaskade im Service (kunde_identity.py):
identity_key > email-Spalte > telefon-Spalte — sonst wuerde ein Kunde,
der erst anruft und spaeter mailt, doppelt angelegt.

Kunden werden nie geloescht (additive-only). Zusammengefuehrte Kunden
bekommen `merged_into_id` gesetzt; Lookups folgen dieser Referenz bis
zum Merge-Ziel, statt den Kunden zu ueberspringen.
"""
from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base


class Kunde(Base):
    """Ein Kunde eines Tenants; `id` = interne Kundennummer."""
    __tablename__ = "kunden"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Anzeigefelder — der Name ist bewusst KEIN Schluessel mehr.
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    telefon: Mapped[str | None] = mapped_column(String(50), nullable=True)
    adresse: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Anlage-Schluessel (Mail > Tel > Slug), Format wie
    # google_drive._kunde_identity_key. Bleibt nach Anlage stehen,
    # auch wenn spaeter Mail/Telefon ergaenzt werden.
    identity_key: Mapped[str] = mapped_column(String(120), nullable=False)

    # Backfill/Import konnte diesen Kunden nicht eindeutig zuordnen —
    # manuelle Pruefung noetig. Wird nie automatisch aufgeloest.
    needs_review: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False,
    )

    # Gesetzt vom Merge-Skript: dieser Kunde ist in einen anderen
    # aufgegangen. Zeigt immer direkt aufs finale Ziel (Ketten werden
    # beim Merge aufgeloest, einstufig gehalten).
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("kunden.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Gesetzt, wenn ein Betroffener nach Art. 17 DSGVO Loeschung verlangt
    # hat: Name, Mail, Telefon und Adresse sind dann geleert, die Zeile
    # bleibt aber stehen, damit Rechnungen und Auftraege ihren Bezug
    # behalten (Aufbewahrungspflicht, Art. 17 Abs. 3 lit. b). Macht den
    # Vorgang nachweisbar und wiederholbar — siehe scripts/dsar.py.
    anonymized_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    # created_at + updated_at via Base

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "identity_key", name="uq_kunden_tenant_identity",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<Kunde id={self.id} name={self.name!r} "
            f"key={self.identity_key!r}>"
        )


__all__ = ["Kunde"]
