"""TenantMaterial — Verbrauchs-Artikel die der Handwerker oft nachbestellt.

Workflow:
1. Handwerker legt einmal an: Name + Bestell-Link (z.B. Toolnation-URL)
2. Bei Bedarf: /bestellen <slug> [menge]
3. Bot zeigt Inline-Telegram-Button mit URL → Tenant klickt → Browser
4. Bestellung wird in material_bestellung als Audit-Log persistiert

KEINE automatische Mail-Bestellung — nur URL-Link-Variante (Sven-Wahl).
"""
from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base


# Bestell-Art-Konstanten
BESTELL_ART_LINK = "link"        # User hat URL-Button geklickt (default)
BESTELL_ART_MANUAL = "manual"    # Manuell erfasst (z.B. Voice spaeter)


class TenantMaterial(Base):
    """Ein Verbrauchs-Artikel den ein Tenant nachbestellt."""

    __tablename__ = "tenant_material"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Identifikation
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    # Bestell-Daten
    bestell_link: Mapped[str] = mapped_column(String(2000), nullable=False)
    lieferant_name: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # Defaults fuer schnelle Bestellung
    einheit: Mapped[str] = mapped_column(String(30), nullable=False, default="Stück")
    standard_menge: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    # Freitext-Notiz (z.B. "fuer Bohrer XYZ")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Soft-Delete via aktiv-Flag (Bestellungs-Historie referenziert weiter)
    aktiv: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", "slug", name="uq_tenant_material_slug"),
        Index("ix_tenant_material_active", "tenant_id", "aktiv"),
    )

    def __repr__(self) -> str:
        return (
            f"<TenantMaterial id={self.id} slug={self.slug!r} "
            f"name={self.name!r}>"
        )


class MaterialBestellung(Base):
    """Audit-Log einer ausgeloesten Bestellung.

    Wird beim Anzeigen des Inline-URL-Buttons geschrieben (nicht beim
    eigentlichen Browser-Klick — den koennen wir technisch nicht
    detektieren). Tenant kann manuell stornieren wenn er den Button
    doch nicht geklickt hat.
    """

    __tablename__ = "material_bestellung"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    material_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenant_material.id", ondelete="SET NULL"),
        nullable=True,
    )
    employee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Bestell-Daten zum Zeitpunkt — eingefroren, falls Material spaeter
    # geaendert wird
    material_name: Mapped[str] = mapped_column(String(200), nullable=False)
    bestell_link: Mapped[str] = mapped_column(String(2000), nullable=False)
    menge: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    einheit: Mapped[str] = mapped_column(String(30), nullable=False, default="Stück")
    bestell_art: Mapped[str] = mapped_column(
        String(20), nullable=False, default=BESTELL_ART_LINK,
    )

    # Optional Metadata (User-Agent vom click-tracking falls wir das mal bauen)
    metadata_json: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)

    __table_args__ = (
        Index("ix_material_bestellung_tenant_time",
              "tenant_id", "created_at"),
        Index("ix_material_bestellung_material",
              "material_id", "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<MaterialBestellung id={self.id} {self.menge}x "
            f"{self.material_name!r}>"
        )


__all__ = [
    "TenantMaterial",
    "MaterialBestellung",
    "BESTELL_ART_LINK",
    "BESTELL_ART_MANUAL",
]
