"""
Beleg: Foto/PDF eines Eingangs-Belegs (Rechnung, Quittung, etc.)
das via Telegram an Lexware hochgeladen wird.

Tenant fotografiert Beleg in Telegram, wir speichern roh in DB
(Audit-Trail) und reichen ihn an Lexware /v1/files weiter.
Tenant prueft + verbucht selbst in Lexware-UI.
"""
import datetime as dt
import uuid

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base


# Status-Konstanten
BELEG_STATUS_PENDING = "pending"        # Im DB gespeichert, Upload steht aus
BELEG_STATUS_UPLOADING = "uploading"    # Lexware-Call laeuft gerade
BELEG_STATUS_UPLOADED = "uploaded"      # Erfolgreich an Lexware uebergeben
BELEG_STATUS_ERROR = "error"            # Upload fehlgeschlagen

# Quellen
BELEG_SOURCE_TELEGRAM = "telegram"
BELEG_SOURCE_MAIL = "mail"
BELEG_SOURCE_API = "api"


class Beleg(Base):
    """Ein an Lexware uebermittelter Beleg."""

    __tablename__ = "belege"

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

    # Telegram-Chat von dem die Anfrage kam (None wenn z.B. Mail)
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # Datei-Daten
    file_data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    file_mime: Mapped[str] = mapped_column(String(100), nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    original_filename: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )

    # Wo kommt der Beleg her
    source: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=BELEG_SOURCE_TELEGRAM
    )

    # User-Notiz (z.B. "Bauhaus-Quittung Schrauben")
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Lexware-IDs (None bis Upload erfolgt)
    lexware_file_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    lexware_voucher_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )

    # Status-Tracking
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default=BELEG_STATUS_PENDING
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    upload_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )

    # Timestamps
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    uploaded_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<Beleg id={self.id} tenant={self.tenant_id} "
            f"status={self.status} source={self.source}>"
        )
