"""AutomationSetting = wie selbstaendig Q eine Funktion bei diesem Tenant ausfuehrt.

Eine Zeile pro (Tenant, Automatisierung). Fehlt die Zeile, gilt
``Automation.default_mode`` aus ``core/features/automations.py`` — es muss
also nichts vorbefuellt werden, wenn ein neuer Betrieb dazukommt oder eine
neue Automatisierung in die Registry wandert.

Getrennt von ``tool_configs``, weil hier eine andere Frage beantwortet wird:
ToolConfig sagt *ob* eine Funktion existiert, AutomationSetting sagt *wie
selbstaendig* sie laufen darf. Eine Automatisierung kann ausserdem mehrere
Tools gleichzeitig steuern (siehe ``Automation.tools``), passt also nicht
1:1 auf einen tool_name.
"""
import uuid

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base
from core.features.automations import ALL_MODES


class AutomationSetting(Base):
    """Automatisierungsgrad einer Funktion fuer einen Tenant."""

    __tablename__ = "automation_settings"

    # Eine Einstellung pro (Tenant, Automatisierung)
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "automation_key", name="uq_tenant_automation"
        ),
        # Schutz gegen Tippfehler im Code: nur die drei bekannten Stufen.
        CheckConstraint(
            "mode IN ('manuell','assistiert','automatisch')",
            name="ck_automation_mode",
        ),
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

    # Key aus AUTOMATIONS (z.B. 'termin_buchen', 'mail_auto_antwort')
    automation_key: Mapped[str] = mapped_column(
        String(50), nullable=False,
        comment="Key aus core/features/automations.py",
    )

    # 'manuell' | 'assistiert' | 'automatisch'
    mode: Mapped[str] = mapped_column(
        String(20), nullable=False,
        comment="manuell = Q handelt nicht, assistiert = Q fragt vorher, "
                "automatisch = Q handelt direkt",
    )

    def __repr__(self) -> str:
        return (
            f"<AutomationSetting {self.automation_key}={self.mode} "
            f"@ {self.tenant_id}>"
        )


# Import-Zeit-Absicherung: DB-CheckConstraint und Python-Registry duerfen
# nicht auseinanderlaufen. Faellt beim Modul-Import sofort auf, nicht erst
# beim ersten INSERT gegen Postgres.
assert set(ALL_MODES) == {"manuell", "assistiert", "automatisch"}, (
    "ALL_MODES und ck_automation_mode sind auseinandergelaufen — "
    "Migration anpassen."
)
