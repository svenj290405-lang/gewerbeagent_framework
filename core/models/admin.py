"""
Admin-Backend-Modelle.

Tabellen:
- admin_users           Login-Konten fuer das /admin Dashboard.
- admin_sessions        Session-Cookies (Server-side).
- admin_audit_log       Alle administrativen Aktionen + fehlgeschlagene Logins.
- admin_login_attempts  Rate-Limit-Buffer fuer /admin/login per IP.
- api_pricing_config    Aktuelle und historische Preise pro Provider/Operation/Unit.
- api_usage_log         Jede API-Verbrauchszeile (token/char/sek) + berechnete Kosten.
"""
from __future__ import annotations

import datetime as dt
import secrets
import uuid
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base


# ---------- Auth ----------

class AdminUser(Base):
    """Admin-Login-Konto."""
    __tablename__ = "admin_users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped["dt.datetime | None"] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_login_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)


def _new_session_token() -> str:
    """Server-side Session-Token (bleibt nur in DB, Cookie haelt nur das Token)."""
    return secrets.token_urlsafe(40)


class AdminSession(Base):
    """Server-side Session - Cookie traegt nur den Token."""
    __tablename__ = "admin_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    token: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True, default=_new_session_token
    )
    csrf_token: Mapped[str] = mapped_column(
        String(64), nullable=False, default=_new_session_token
    )
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_activity_at: Mapped["dt.datetime"] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped["dt.datetime"] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class AdminAuditLog(Base):
    """Audit-Log: Login, Logout, Settings-Aenderung, Cost-Lookup, Failed-Login etc."""
    __tablename__ = "admin_audit_log"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped["uuid.UUID | None"] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    action: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    target: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(500), nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class AdminLoginAttempt(Base):
    """Buffer fuer /admin/login Rate-Limiting per IP."""
    __tablename__ = "admin_login_attempts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    ip_address: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    email_tried: Mapped[str | None] = mapped_column(String(255), nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    attempted_at: Mapped["dt.datetime"] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )

    __table_args__ = (
        Index("ix_login_attempts_ip_time", "ip_address", "attempted_at"),
    )


# ---------- API-Usage + Pricing ----------

# Bekannte Units
UNIT_INPUT_TOKEN = "input_token"
UNIT_OUTPUT_TOKEN = "output_token"
UNIT_CACHED_INPUT_TOKEN = "cached_input_token"
UNIT_CHARACTER = "character"
UNIT_SECOND = "second"
UNIT_REQUEST = "request"
UNIT_MAIL_SEND = "mail_send"

# Bekannte Provider
PROVIDER_GEMINI = "gemini"
PROVIDER_VERTEX = "vertex"
PROVIDER_ELEVENLABS = "elevenlabs"
PROVIDER_DEEPGRAM = "deepgram"
PROVIDER_SIPGATE = "sipgate"
PROVIDER_MICROSOFT = "microsoft"
PROVIDER_BREVO = "brevo"
PROVIDER_LEXWARE = "lexware"
PROVIDER_TELEGRAM = "telegram"


class ApiPricingConfig(Base):
    """
    Preis-Konfiguration. Eine Zeile pro (provider, operation, unit) und
    Geltungszeitraum. valid_to = NULL bedeutet aktuell gueltig.
    """
    __tablename__ = "api_pricing_config"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    # Operation = freier Bezeichner, z.B. "gemini-2.5-flash" oder "tts-default"
    # Kann NULL sein wenn der Preis fuer den ganzen Provider gilt.
    operation: Mapped[str | None] = mapped_column(String(80), nullable=True)
    unit: Mapped[str] = mapped_column(String(50), nullable=False)
    price_per_unit_eur: Mapped[Decimal] = mapped_column(
        Numeric(18, 10), nullable=False
    )
    valid_from: Mapped["dt.datetime"] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    valid_to: Mapped["dt.datetime | None"] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped["uuid.UUID | None"] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__ = (
        Index(
            "ix_pricing_lookup",
            "provider", "operation", "unit", "valid_from",
        ),
    )


class ApiUsageLog(Base):
    """
    Eine Zeile pro API-Aufruf-Verbrauch. Kosten werden beim Insert anhand
    der gerade gueltigen Pricing-Config berechnet und mit eingefroren.
    """
    __tablename__ = "api_usage_log"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped["uuid.UUID | None"] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    operation: Mapped[str | None] = mapped_column(String(80), nullable=True)
    unit: Mapped[str] = mapped_column(String(50), nullable=False)
    units_consumed: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    price_per_unit_eur: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 10), nullable=True
    )
    cost_eur: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, default=Decimal("0")
    )
    pricing_config_id: Mapped["uuid.UUID | None"] = mapped_column(
        UUID(as_uuid=True), ForeignKey("api_pricing_config.id", ondelete="SET NULL"),
        nullable=True,
    )
    request_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)

    __table_args__ = (
        Index("ix_usage_tenant_time", "tenant_id", "created_at"),
        Index("ix_usage_provider_time", "provider", "created_at"),
    )


__all__ = [
    "AdminUser",
    "AdminSession",
    "AdminAuditLog",
    "AdminLoginAttempt",
    "ApiPricingConfig",
    "ApiUsageLog",
    "UNIT_INPUT_TOKEN",
    "UNIT_OUTPUT_TOKEN",
    "UNIT_CACHED_INPUT_TOKEN",
    "UNIT_CHARACTER",
    "UNIT_SECOND",
    "UNIT_REQUEST",
    "UNIT_MAIL_SEND",
    "PROVIDER_GEMINI",
    "PROVIDER_VERTEX",
    "PROVIDER_ELEVENLABS",
    "PROVIDER_DEEPGRAM",
    "PROVIDER_SIPGATE",
    "PROVIDER_MICROSOFT",
    "PROVIDER_BREVO",
    "PROVIDER_LEXWARE",
    "PROVIDER_TELEGRAM",
]
