"""
OAuthToken = verschluesselt gespeicherter API-Zugang eines Tenants.
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base
from core.security.encryption import decrypt, encrypt


class OAuthToken(Base):
    __tablename__ = "oauth_tokens"

    __table_args__ = (
        UniqueConstraint("tenant_id", "provider", name="uq_tenant_provider"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
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
        return f"<OAuthToken {self.provider} @ {self.tenant_id}>"
