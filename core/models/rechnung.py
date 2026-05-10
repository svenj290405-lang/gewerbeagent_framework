"""
Rechnung: Telegram-Wizard zur Rechnungserstellung in Lexware.

Tenant tippt oder spricht: 'Rechnung an Frau Mueller in Trier,
Moebelmontage 350 Euro brutto'.
Gemini extrahiert Felder, Bot zeigt Vorschau, Tenant bestaetigt,
wir legen Lexware-Draft an. Tenant prueft + finalisiert in Lexware-UI.
Optional: PDF aus Lexware holen + via Brevo an Kunden mailen.
"""
import datetime as dt
import decimal
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base


# Status-Konstanten
RECHNUNG_STATUS_EXTRACTING = "extracting"      # Gemini-Call laeuft
RECHNUNG_STATUS_PREVIEWING = "previewing"      # Vorschau angezeigt, wartet auf Best.
RECHNUNG_STATUS_CREATING = "creating"          # Lexware-Call laeuft
RECHNUNG_STATUS_DRAFTED = "drafted"            # Draft in Lexware fertig
RECHNUNG_STATUS_MAIL_SENT = "mail_sent"        # PDF per Mail an Kunde verschickt
RECHNUNG_STATUS_BEZAHLT = "bezahlt"            # Lexware hat voucherStatus=paid gemeldet
RECHNUNG_STATUS_ERROR = "error"
RECHNUNG_STATUS_CANCELLED = "cancelled"        # User hat abgebrochen

# Lexware voucherStatus-Werte die wir als "bezahlt" werten.
# 'paid' ist der dokumentierte Standardwert; 'paidoff' kommt in einigen
# alten Tenants vor. Beide Schreibweisen abdecken um robust zu sein.
LEXWARE_PAID_STATES = frozenset({"paid", "paidoff"})

# Lexware voucherStatus-Werte die "teilweise bezahlt" bedeuten.
# Wird im /rechnungen_anzeigen mit eigenem Icon angezeigt aber nicht
# als 'bezahlt' gewertet — der Tenant entscheidet ob er die Restzahlung
# anmahnt oder den Restbetrag abschreibt.
LEXWARE_PARTIAL_PAID_STATES = frozenset({"partiallypaid", "partly_paid"})

# Eingabe-Typen
RECHNUNG_INPUT_TEXT = "text"
RECHNUNG_INPUT_VOICE = "voice"


class Rechnung(Base):
    """Eine ueber Telegram angelegte Rechnung."""

    __tablename__ = "rechnungen"

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

    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # Eingabe-Audit-Trail
    input_type: Mapped[str] = mapped_column(String(10), nullable=False)
    raw_input_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Komplettes Gemini-JSON (fuer Debugging/Replay)
    extracted_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Strukturierte Felder
    kunde_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    kunde_ort: Mapped[str | None] = mapped_column(String(255), nullable=True)
    kunde_strasse: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    kunde_plz: Mapped[str | None] = mapped_column(String(20), nullable=True)
    kunde_email: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    leistung_titel: Mapped[str | None] = mapped_column(
        String(500), nullable=True
    )
    leistung_beschreibung: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    betrag_brutto_eur: Mapped[decimal.Decimal | None] = mapped_column(
        Numeric(precision=10, scale=2), nullable=True
    )

    # Lexware-IDs (None bis Draft erstellt)
    lexware_contact_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    lexware_invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    lexware_voucher_number: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )

    # Status-Tracking
    status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        server_default=RECHNUNG_STATUS_EXTRACTING,
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Mail-Versand (Phase A2)
    mail_sent_to: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    mail_sent_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Bezahl-Tracking (Phase A3 — Lexware-Polling alle 30 Min).
    # Migration: j2c8e5f1a4d6_rechnung_bezahl_tracking.
    bezahlt_am: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lexware_voucher_status: Mapped[str | None] = mapped_column(
        String(30), nullable=True
    )
    last_paid_check_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    paid_notification_sent: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default="false",
    )

    # Timestamps
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    drafted_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # Phase-4-Multi-Mitarbeiter: wer ist verantwortlich (z.B. fuer
    # Bezahl-Tracking, Mahnungen). Backfill auf Default-Employee,
    # neue Rechnungen setzen das beim Anlegen. SET NULL bei deaktivierten
    # Mitarbeitern.
    responsible_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )

    def __repr__(self) -> str:
        return (
            f"<Rechnung id={self.id} tenant={self.tenant_id} "
            f"status={self.status} kunde={self.kunde_name!r}>"
        )
