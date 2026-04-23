"""
Tenant = ein Kunde des Frameworks (ein Handwerksbetrieb).

Jeder Tenant hat:
- Eine eindeutige ID (UUID, nicht numerisch, damit IDs nicht erratbar sind)
- Einen Slug (z.B. "dietz") für URLs und Admin-Befehle
- Firmen-Stammdaten
- Status (active/inactive/suspended)
- Relationships zu ToolConfigs und OAuthTokens
"""
import uuid
from enum import Enum
from typing import TYPE_CHECKING

from sqlalchemy import String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database.base import Base

if TYPE_CHECKING:
    from core.models.oauth_token import OAuthToken
    from core.models.tool_config import ToolConfig


class TenantStatus(str, Enum):
    """Lifecycle eines Kunden."""
    ONBOARDING = "onboarding"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    ARCHIVED = "archived"


class Tenant(Base):
    """Ein Kunde (Handwerksbetrieb) des Frameworks."""
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    slug: Mapped[str] = mapped_column(
        String(50), unique=True, nullable=False, index=True,
    )

    # Firmen-Stammdaten
    company_name: Mapped[str] = mapped_column(String(200), nullable=False)
    contact_name: Mapped[str] = mapped_column(String(200), nullable=False)
    contact_email: Mapped[str] = mapped_column(String(200), nullable=False)
    contact_phone: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Status & Billing
    status: Mapped[TenantStatus] = mapped_column(
        String(20), default=TenantStatus.ONBOARDING, nullable=False, index=True
    )

    # Freitext für interne Notizen
    notes: Mapped[str | None] = mapped_column(String(2000), nullable=True)

    # --- Relationships ---

    # Alle ToolConfigs dieses Tenants (automatisch geladen)
    tool_configs: Mapped[list["ToolConfig"]] = relationship(
        back_populates="tenant",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    # Alle OAuth-Tokens dieses Tenants (Google, Microsoft, etc.)
    oauth_tokens: Mapped[list["OAuthToken"]] = relationship(
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<Tenant {self.slug} ({self.company_name})>"