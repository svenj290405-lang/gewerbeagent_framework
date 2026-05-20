"""TenantKundeDrive — Mapping (Tenant, Kunde) → Google-Drive-Ordner.

Pro Kunde wird beim ersten Upload (`/archiv <name>`) ein Ordner im
Drive des Tenants erstellt. Diese Tabelle persistiert den Folder-Lookup
damit Folge-Uploads in den gleichen Ordner gehen und /briefing den
Link kennt ohne Drive-API anzufragen.

Kunden-Identifikation: `kunde_key` = stabile Identitaet —
  "email:<mail>" > "tel:<normalisierte-nummer>" > slugify(name) (Fallback).
So teilen sich NICHT mehr zwei Kunden mit gleichem Namen einen Ordner
(unterschiedliche Mail/Telefon -> unterschiedliche Ordner), und dieselbe
Person (gleiche Mail/Telefon) landet zuverlaessig im selben Ordner —
auch wenn der Name minimal abweicht. Der Ordner-ANZEIGENAME ist der
volle Kundenname. `kunde_email`/`kunde_telefon` werden zur Referenz
mitgespeichert.

Soft-Delete: nicht noetig. Wenn der Tenant einen Drive-Ordner manuell
loescht, gibt die naechste Drive-API einen 404 zurueck — der Helper
loescht die DB-Zeile und erstellt lazy einen neuen Ordner.
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


class TenantKundeDrive(Base):
    """Mapping fuer Tenant-Kunde-Drive-Folder."""
    __tablename__ = "tenant_kunde_drive"

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
    kunde_key: Mapped[str] = mapped_column(String(120), nullable=False)
    kunde_name: Mapped[str] = mapped_column(String(200), nullable=False)
    # Identitaets-Referenz (woraus der kunde_key gebildet wurde) —
    # zur Nachvollziehbarkeit + um bei Bedarf umzuschluesseln.
    kunde_email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    kunde_telefon: Mapped[str | None] = mapped_column(String(40), nullable=True)

    # Drive-Folder
    drive_folder_id: Mapped[str] = mapped_column(String(100), nullable=False)
    drive_folder_url: Mapped[str] = mapped_column(String(500), nullable=False)

    # Statistik
    upload_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False,
    )
    last_upload_at: Mapped["dt.datetime | None"] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    # created_at + updated_at via Base

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "kunde_key", name="uq_tenant_kunde_drive_key",
        ),
        Index(
            "ix_tenant_kunde_drive_last_upload",
            "tenant_id", "last_upload_at",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<TenantKundeDrive id={self.id} kunde={self.kunde_name!r} "
            f"folder={self.drive_folder_id}>"
        )


__all__ = ["TenantKundeDrive"]
