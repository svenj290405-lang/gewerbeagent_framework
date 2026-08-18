"""EmployeePermission = Abweichung eines Mitarbeiters von seiner Rollen-Vorlage.

Eine Zeile pro (Mitarbeiter, Recht) — und zwar NUR, wenn der Inhaber
bewusst vom Preset abweicht. Fehlt die Zeile, gilt
``ROLLEN_PRESETS[employee.role]`` aus ``core/features/permissions.py``.

Warum sparse und nicht ein Snapshot aller Rechte pro Mitarbeiter:
Presets entwickeln sich mit dem Produkt weiter. Kommt spaeter ein Recht
dazu, sollen alle Buerokraefte es automatisch in der Preset-Auspraegung
bekommen. Sparse Rows liefern das gratis; ein Snapshot wuerde bei jedem
neuen Key eine Datenmigration erzwingen.

Gebaut wie ``automation_setting.py`` (Registry im Code, Default im
Dataclass, Abweichung in der DB, 60s-Cache mit Invalidierung).

Bewusst KEIN CHECK-Constraint auf ``permission_key``: anders als die drei
Automatisierungsstufen aendert sich die Rechte-Liste haeufiger, und ein
verwaister Key aus einer alten Version wird beim Lesen einfach ignoriert
statt ein Schema-Update zu erzwingen.
"""
import uuid

from sqlalchemy import Boolean, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base


class EmployeePermission(Base):
    """Ein einzelnes Recht, das vom Rollen-Preset abweicht."""

    __tablename__ = "employee_permissions"

    __table_args__ = (
        UniqueConstraint(
            "employee_id", "permission_key", name="uq_emp_permission"
        ),
        Index("ix_emp_perm_tenant", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    permission_key: Mapped[str] = mapped_column(
        String(50), nullable=False,
        comment="Key aus core/features/permissions.py",
    )

    # True = zusaetzlich gewaehrt, False = trotz Rolle entzogen.
    allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)

    def __repr__(self) -> str:
        zeichen = "+" if self.allowed else "-"
        return (
            f"<EmployeePermission {zeichen}{self.permission_key} "
            f"@ {self.employee_id}>"
        )
