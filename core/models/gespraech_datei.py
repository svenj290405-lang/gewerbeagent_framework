"""GespraechDatei — Foto oder Visualisierung, die zu einem Kundengespraech gehoert.

Die Bytes liegen NICHT hier: Fotos landen im Drive-Kundenordner (wie das
uebrige Archiv, angezeigt ueber den Proxy /archiv/datei/{id}),
Visualisierungen in ``visualisierungen.result_image_data``. Diese Tabelle
ist die Zuordnung „welches Bild gehoert zu welchem Gespraech" — mehr nicht.
So bleibt ein Bild auch dann im Kundenordner auffindbar, wenn das Gespraech
nach Ablauf der Aufbewahrungsfrist geloescht wird.
"""
from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base

# Woher das Bild kommt.
GESPRAECH_DATEI_FOTO = "foto"                    # vom Handwerker hochgeladen
GESPRAECH_DATEI_VISUALISIERUNG = "visualisierung"  # von Q gerendert


class GespraechDatei(Base):
    __tablename__ = "gespraech_dateien"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    gespraech_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("kundengespraeche.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    typ: Mapped[str] = mapped_column(String(20), nullable=False)

    # Foto: Drive-Datei-ID + Web-Link. Die ID ist zugleich der Schluessel
    # fuer den Anzeige-Proxy und fuer Mail-Anhaenge.
    drive_file_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    drive_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    # Visualisierung: Verweis auf die gerenderte Fassung.
    visualisierung_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("visualisierungen.id", ondelete="CASCADE"),
        nullable=True,
    )

    dateiname: Mapped[str | None] = mapped_column(String(300), nullable=True)
    mime: Mapped[str | None] = mapped_column(String(100), nullable=True)

    def __repr__(self) -> str:
        return (f"<GespraechDatei id={self.id} typ={self.typ!r} "
                f"gespraech={self.gespraech_id}>")
