"""Anfrage-Formular-Modelle: Token + Response.

Workflow:
1. Bot empfaengt RELEVANT_KUNDE Mail
2. Bot generiert AnfrageToken mit Expiry +7 Tage
3. Bot sendet Mail an Kunde mit Link https://gewerbeagent.de/anfrage/{token}
4. Kunde fuellt Web-Formular aus
5. AnfrageResponse wird gespeichert + Telegram-Push an Tenant
"""
from __future__ import annotations

import datetime as dt
import secrets
import uuid

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database.base import Base


# Anfrage-Typen (Tenant-Branchen-spezifisch, Default 'allgemein')
ANFRAGE_TYP_TISCHLER = "tischler"
ANFRAGE_TYP_ALLGEMEIN = "allgemein"


# Bearbeitungs-Status fuer eingegangene Formulare (AnfrageResponse).
# Wird vom Handwerker per Inline-Button im Telegram-Push gesetzt.
# Heartbeat-Cron pingt morgens nach wenn Antworten > 12h auf 'neu' /
# 'in_bearbeitung' stehen.
FORMULAR_STATUS_NEU = "neu"
FORMULAR_STATUS_IN_BEARBEITUNG = "in_bearbeitung"
FORMULAR_STATUS_ERLEDIGT = "erledigt"
FORMULAR_STATUS_ABGELEHNT = "abgelehnt"
FORMULAR_STATUS_OFFEN = {FORMULAR_STATUS_NEU, FORMULAR_STATUS_IN_BEARBEITUNG}
FORMULAR_STATUS_VALID = {
    FORMULAR_STATUS_NEU,
    FORMULAR_STATUS_IN_BEARBEITUNG,
    FORMULAR_STATUS_ERLEDIGT,
    FORMULAR_STATUS_ABGELEHNT,
}
FORMULAR_STATUS_LABEL = {
    FORMULAR_STATUS_NEU: "🆕 Neu",
    FORMULAR_STATUS_IN_BEARBEITUNG: "📝 In Bearbeitung",
    FORMULAR_STATUS_ERLEDIGT: "✅ Erledigt",
    FORMULAR_STATUS_ABGELEHNT: "❌ Abgelehnt",
}


def _new_token() -> str:
    """Generiert URL-sicheren Token (22 Zeichen ASCII).

    16 Bytes Entropie = 128 Bit, sicher gegen Brute-Force, deutlich
    kuerzer im URL-Display. Kunden-feedback 2026-05-17: vorherige
    43-Zeichen-Tokens sahen in der Adressleiste zu lang/scammig aus.
    """
    return secrets.token_urlsafe(16)


class AnfrageToken(Base):
    __tablename__ = "anfrage_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True, default=_new_token)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    kunde_email: Mapped[str] = mapped_column(String(255), nullable=False)
    kunde_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Voice-Session-Lookup: bei Anrufen pflanzen wir die normalisierte
    # Telefonnummer des Anrufers hier hinein, damit _handle_buche_termin
    # die kunde_email per Telefon-Match wiederfinden und ans Kalender-
    # Event haengen kann. NULL bei Mail-getriebenen Tokens.
    # Index (tenant_id, kunde_telefon) WHERE NOT NULL — siehe Migration
    # a9k2m4n6p8q1.
    kunde_telefon: Mapped[str | None] = mapped_column(String(50), nullable=True)
    anfrage_typ: Mapped[str] = mapped_column(String(50), nullable=False, default=ANFRAGE_TYP_ALLGEMEIN)
    original_subject: Mapped[str | None] = mapped_column(String(500), nullable=True)
    original_message_id: Mapped[str | None] = mapped_column(String(500), nullable=True)
    expires_at: Mapped["dt.datetime"] = mapped_column(DateTime(timezone=True), nullable=False)
    submitted_at: Mapped["dt.datetime | None"] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped["dt.datetime"] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    responses: Mapped[list["AnfrageResponse"]] = relationship(
        "AnfrageResponse", back_populates="token", cascade="all, delete-orphan"
    )


class AnfrageResponse(Base):
    __tablename__ = "anfrage_responses"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    token_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("anfrage_tokens.id", ondelete="CASCADE"), nullable=False
    )
    antworten: Mapped[dict] = mapped_column(JSONB, nullable=False)
    submitted_ip: Mapped[str | None] = mapped_column(String(50), nullable=True)
    submitted_at: Mapped["dt.datetime"] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Phase-4-Multi-Mitarbeiter: wer bekommt die Anfrage zugewiesen.
    # Phase-5-Skill-Router setzt das anhand der Antwort-Inhalte; bis
    # dahin Backfill auf Default-Employee.
    assigned_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )

    # Bearbeitungs-Status (siehe FORMULAR_STATUS_* Konstanten oben).
    # Default 'neu' beim Insert. Aktuell nicht aktiv gepflegt — die
    # Eingangs-Tracking-Feature wurde rausgenommen zugunsten Drive-
    # Archiv. Spalten bleiben in der DB damit historische Daten nicht
    # verloren gehen; ggf. spaeter via eigener Migration entfernen.
    bearbeitungs_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=FORMULAR_STATUS_NEU,
        server_default=FORMULAR_STATUS_NEU,
    )
    bearbeitet_at: Mapped["dt.datetime | None"] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    bearbeitet_by_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="SET NULL"),
        nullable=True,
    )

    token: Mapped["AnfrageToken"] = relationship("AnfrageToken", back_populates="responses")
