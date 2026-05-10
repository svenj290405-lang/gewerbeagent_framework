"""GeocodeCache — Adresse → lat/lon, gecached fuer ORS-Free-Tier.

Pelias-Geocoding bei OpenRouteService kostet 1 Request pro Adresse.
Free-Tier ist 2.000 Requests/Tag, gemeinsam fuer geocode + matrix.
Ohne Cache wuerden wir bei jedem Slot-Filter denselben Kunden neu
geocoden — schnell weg.

Address-Key ist SHA-256 der normalisierten Adresse (siehe
core/integrations/openrouteservice.normalize_address()), damit
geringfuegige Schreibvarianten ('Hauptstr.' vs 'Hauptstrasse')
auf denselben Cache-Eintrag mappen.
"""
import datetime as dt
import decimal
import uuid

from sqlalchemy import (
    DateTime,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base


class GeocodeCache(Base):
    """Eine geocachte Adresse → lat/lon."""

    __tablename__ = "geocode_cache"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    address_key: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True,
    )
    address_normalized: Mapped[str] = mapped_column(
        String(500), nullable=False,
    )
    lat: Mapped[decimal.Decimal] = mapped_column(
        Numeric(precision=9, scale=6), nullable=False,
    )
    lon: Mapped[decimal.Decimal] = mapped_column(
        Numeric(precision=9, scale=6), nullable=False,
    )
    formatted: Mapped[str | None] = mapped_column(
        String(500), nullable=True,
    )
    geocoded_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    hit_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0,
    )

    def __repr__(self) -> str:
        return (
            f"<GeocodeCache key={self.address_key[:8]}.. "
            f"lat={self.lat} lon={self.lon}>"
        )
