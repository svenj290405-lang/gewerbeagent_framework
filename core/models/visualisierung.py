"""
Visualisierung: KI-generierte Raumdarstellung.

Tischler nimmt ein Foto der Stelle wo z.B. eine Treppe hin soll, beschreibt
was rein soll, und Q rendert eine fotorealistische Vorschau via Gemini.
Bilder werden direkt in der DB gespeichert (BYTEA), atomar mit dem Status.
"""
import datetime as dt
import uuid

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    LargeBinary,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base


# Status-Konstanten
VIZ_STATUS_PENDING = "pending"          # neu, noch keine Daten
VIZ_STATUS_GENERATING = "generating"    # Gemini-Call laeuft
VIZ_STATUS_DONE = "done"                # Bild fertig
VIZ_STATUS_FAILED = "failed"            # Generierung gescheitert
VIZ_STATUS_SENT = "sent"                # An Kunden gemailt


class Visualisierung(Base):
    """Ein Visualisierungs-Auftrag eines Tischlers."""

    __tablename__ = "visualisierungen"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Telegram-Chat von dem die Anfrage kam
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # An wen die Visualisierung gemailt wird (optional, kann auch nur intern bleiben)
    kunde_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    kunde_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Bilder als BYTEA in DB (atomar, einfach loeschbar mit Tenant)
    original_image_data: Mapped[bytes | None] = mapped_column(
        LargeBinary, nullable=True
    )
    result_image_data: Mapped[bytes | None] = mapped_column(
        LargeBinary, nullable=True
    )

    # Was der Tischler beschrieben hat (Prompt fuer Gemini)
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Status-Tracking
    status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        server_default=VIZ_STATUS_PENDING,
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    completed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    def __repr__(self) -> str:
        return f"<Visualisierung id={self.id} tenant={self.tenant_id} status={self.status}>"
