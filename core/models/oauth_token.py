"""
OAuthToken = verschluesselt gespeicherter API-Zugang eines Tenants
oder eines konkreten Mitarbeiters.

Phase 1 Multi-OAuth: jeder Mitarbeiter (employees-Tabelle) kann
seinen eigenen Token pro Provider haben (employee_id != NULL).
Eintraege mit employee_id = NULL sind Legacy / tenant-weite Tokens
und werden ueber die zwei partial-unique-Indizes
(uq_oauth_tenant_provider_when_no_employee bzw.
uq_oauth_employee_provider) eindeutig gehalten — siehe Migration
q3l7h2j5g9k4.

Der alte Constraint uq_tenant_provider (tenant_id, provider) wurde in
Migration s5n9j4l7q3r8 (M2) gedroppt, nachdem der Code stabil
employee_id-aware schreibt — sonst koennte ein zweiter Mitarbeiter
keinen eigenen Provider-Token verbinden.
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base
from core.security.encryption import decrypt, encrypt


class OAuthToken(Base):
    __tablename__ = "oauth_tokens"

    # Eindeutigkeit wird durch zwei partial-unique-Indizes erzwungen
    # (Migration q3l7h2j5g9k4): uq_oauth_employee_provider (employee_id,
    # provider) und uq_oauth_tenant_provider_when_no_employee
    # (tenant_id, provider WHERE employee_id IS NULL). Der alte
    # uq_tenant_provider wurde in Migration s5n9j4l7q3r8 (M2) gedroppt,
    # damit mehrere Mitarbeiter pro Tenant je einen eigenen Provider-
    # Token (eigener Kalender) verbinden koennen.

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Phase-1-Multi-OAuth: Token gehoert einem konkreten Mitarbeiter.
    # NULL = Legacy / tenant-weiter Token (Backward-Compat-Lookup).
    # Neue Tokens werden immer mit employee_id geschrieben.
    employee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=True, index=True,
    )

    provider: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True,
    )

    scopes: Mapped[str] = mapped_column(String(1000), nullable=False, default="")

    _refresh_token_encrypted: Mapped[str] = mapped_column(
        "refresh_token_encrypted", String(2000), nullable=False
    )

    _access_token_encrypted: Mapped[str | None] = mapped_column(
        "access_token_encrypted", String(2000), nullable=True
    )

    access_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    account_email: Mapped[str | None] = mapped_column(String(200), nullable=True)

    @property
    def refresh_token(self) -> str:
        return decrypt(self._refresh_token_encrypted)

    @refresh_token.setter
    def refresh_token(self, value: str) -> None:
        self._refresh_token_encrypted = encrypt(value)

    @property
    def access_token(self) -> str | None:
        if not self._access_token_encrypted:
            return None
        return decrypt(self._access_token_encrypted)

    @access_token.setter
    def access_token(self, value: str | None) -> None:
        self._access_token_encrypted = encrypt(value) if value else None

    def __repr__(self) -> str:
        emp = f" emp={self.employee_id}" if self.employee_id else " (tenant-wide)"
        return f"<OAuthToken {self.provider} @ {self.tenant_id}{emp}>"
