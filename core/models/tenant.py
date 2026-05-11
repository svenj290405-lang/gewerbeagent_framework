"""
Tenant = ein Kunde des Frameworks (ein Handwerksbetrieb).

Jeder Tenant hat:
- Eine eindeutige ID (UUID, nicht numerisch, damit IDs nicht erratbar sind)
- Einen Slug (z.B. "dietz") für URLs und Admin-Befehle
- Firmen-Stammdaten
- Status (active/inactive/suspended)
- Relationships zu ToolConfigs und OAuthTokens
"""
import decimal
import uuid
from enum import Enum
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Integer, Numeric, String
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

    # Telegram-Bot-Chat-ID (wird beim Onboarding via QR-Code gesetzt)
    telegram_chat_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, index=True
    )

    # Branche fuer Agent-Mapping (tischler, sanitaer, elektrik, ...)
    branche: Mapped[str | None] = mapped_column(
        String(50), nullable=True, index=True
    )

    # Voice-Phone-Number fuer Tenant-Routing bei eingehenden Calls
    # (E.164: +492187973998912)
    voice_phone_number: Mapped[str | None] = mapped_column(
        String(30), nullable=True, unique=True, index=True
    )

    # Werkstatt-Adresse — Heimat-Punkt fuer Smart-Termin-Routing.
    # Migration k4f1a8b2d6e3. Pro Tenant ueber /werkstatt-Wizard gepflegt.
    heimat_strasse: Mapped[str | None] = mapped_column(String(255), nullable=True)
    heimat_plz: Mapped[str | None] = mapped_column(String(10), nullable=True)
    heimat_ort: Mapped[str | None] = mapped_column(String(200), nullable=True)
    heimat_lat: Mapped[decimal.Decimal | None] = mapped_column(
        Numeric(precision=9, scale=6), nullable=True
    )
    heimat_lon: Mapped[decimal.Decimal | None] = mapped_column(
        Numeric(precision=9, scale=6), nullable=True
    )
    # Minuten Puffer ueber die ORS-Fahrtzeit hinaus (Material laden,
    # Haende waschen, Kunde verabschieden). Default 15.
    fahrtzeit_puffer_min: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="15", default=15,
    )

    # Paket-Tier — bestimmt welche Features per Default aktiv sind.
    # Werte: 'basis' | 'pro' | 'enterprise' | 'custom'.
    # 'custom' = Sven hat einzelne Features manuell getoggled, weicht
    # vom vordefinierten Paket ab. ToolConfig.enabled bleibt die Quelle
    # der Wahrheit fuer einzelne Features — package_tier ist nur die
    # menschen-lesbare Zusammenfassung.
    # Default 'pro' damit bestehende Tenants beim Backfill nicht
    # versehentlich downgraded werden (siehe scripts/backfill_tenant_features).
    package_tier: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="pro", default="pro",
    )

    # DSGVO-Retention in Tagen. Steuert dsgvo_cleanup_cron.
    # Range: 7-365 (sanftes Limit im Admin-Form). Default 90.
    # Phase B4: bisher globaler RETENTION_DAYS=14 — jetzt pro Tenant.
    data_retention_days: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="90", default=90,
    )

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