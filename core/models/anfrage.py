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


def _new_token() -> str:
    """Generiert URL-sicheren Token (43 Zeichen ASCII)."""
    return secrets.token_urlsafe(32)


class AnfrageToken(Base):
    __tablename__ = "anfrage_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True, default=_new_token)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    kunde_email: Mapped[str] = mapped_column(String(255), nullable=False)
    kunde_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
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

    token: Mapped["AnfrageToken"] = relationship("AnfrageToken", back_populates="responses")
