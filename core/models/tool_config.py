"""
ToolConfig = welches Plugin ist bei welchem Kunden aktiv, mit welchen Einstellungen.

Ein Plugin wird für einen Tenant freigeschaltet, indem eine Zeile mit
enabled=True hier eingefügt wird. Die config-Spalte enthält plugin-spezifische
Einstellungen als JSON (Arbeitszeiten, Ordner-IDs, Standard-Antworten etc.).
"""
import uuid

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database.base import Base


class ToolConfig(Base):
    """Aktivierung + Konfiguration eines Plugins für einen Tenant."""
    __tablename__ = "tool_configs"

    # Eindeutige Kombination aus (tenant, tool) — ein Tool pro Tenant nur einmal
    __table_args__ = (
        UniqueConstraint("tenant_id", "tool_name", name="uq_tenant_tool"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Welcher Kunde (Fremdschlüssel auf tenants)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Welches Plugin (entspricht Ordnername unter plugins/)
    tool_name: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True,
        comment="z.B. 'kalender', 'belege', 'mail_auto'",
    )

    # Aktiv oder deaktiviert?
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, index=True
    )

    # Plugin-spezifische Konfiguration als JSON
    # Beispiel Kalender: {"arbeitszeiten_start": "08:00", "arbeitszeiten_ende": "17:00"}
    # JSONB ist Postgres-spezifisch: indexierbar, durchsuchbar, effizient
    config: Mapped[dict] = mapped_column(
        JSONB, default=dict, nullable=False,
        comment="Plugin-spezifische Einstellungen",
    )

    # Beziehung zum Tenant (optional, ermöglicht tenant.tool_configs)
    tenant: Mapped["Tenant"] = relationship(back_populates="tool_configs")  # noqa: F821

    def __repr__(self) -> str:
        status = "✓" if self.enabled else "✗"
        return f"<ToolConfig {status} {self.tool_name} @ {self.tenant_id}>"