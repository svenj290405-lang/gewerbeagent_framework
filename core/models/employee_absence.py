"""EmployeeAbsence — Krank/Urlaub/Abwesenheit pro Mitarbeiter.

Pro Eintrag: ein durchgehender Zeitraum mit Typ (krank|urlaub|sonstiges).
end_date NULL = open-ended ("krank, weiss noch nicht wie lange").

Verwendet von:
- /krank, /urlaub, /abwesend, /zurueck Telegram-Wizards.
- core.routing.employee_router: choose_employee() filtert mit
  is_employee_working_at(emp, target_dt) abwesende Mitarbeiter
  raus (siehe target_datetime-Param).
- core.integrations.absence_redistribution: Cron + Trigger bei /krank
  liest get_active_absences und re-routed Kalender-Termine.

Idempotenz: pro (employee_id) ist immer hoechstens 1 offene Absence
relevant — wir wollen das nicht ueber Constraint erzwingen (User kann
Urlaub planen + parallel kurz krank werden), aber Helper liefern
immer "die heute relevante" via ORDER BY start_date DESC LIMIT 1.
"""
from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database.base import Base

# Absence-Typen (string-konstant, gespiegelt in der DB-CHECK-Constraint)
ABSENCE_KRANK = "krank"
ABSENCE_URLAUB = "urlaub"
ABSENCE_SONSTIGES = "sonstiges"
ALLE_ABSENCE_TYPES = (ABSENCE_KRANK, ABSENCE_URLAUB, ABSENCE_SONSTIGES)


class EmployeeAbsence(Base):
    """Ein durchgehender Abwesenheits-Zeitraum eines Mitarbeiters."""

    __tablename__ = "employee_absences"

    __table_args__ = (
        CheckConstraint(
            "absence_type IN ('krank','urlaub','sonstiges')",
            name="ck_absence_type",
        ),
        Index("ix_absence_employee_start", "employee_id", "start_date"),
        Index("ix_absence_tenant_active", "tenant_id", "start_date", "end_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
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
    )
    start_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    end_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    absence_type: Mapped[str] = mapped_column(String(20), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    employee = relationship(
        "Employee", foreign_keys=[employee_id], lazy="joined",
    )

    def __repr__(self) -> str:
        end = self.end_date.isoformat() if self.end_date else "open"
        return (
            f"<EmployeeAbsence {self.absence_type} "
            f"emp={self.employee_id} {self.start_date}..{end}>"
        )

    def covers(self, target: dt.date) -> bool:
        """True wenn `target` innerhalb [start_date, end_date] liegt
        (oder >= start_date wenn open-ended)."""
        if target < self.start_date:
            return False
        if self.end_date is None:
            return True
        return target <= self.end_date


# ----------------------------------------------------------------------
# Helper-Funktionen
# ----------------------------------------------------------------------


async def is_employee_absent_on(
    employee_id: uuid.UUID, target_date: dt.date,
) -> bool:
    """True wenn der Mitarbeiter an target_date eine aktive Absence hat."""
    from core.database import AsyncSessionLocal
    async with AsyncSessionLocal() as session:
        stmt = select(EmployeeAbsence).where(
            EmployeeAbsence.employee_id == employee_id,
            EmployeeAbsence.start_date <= target_date,
        )
        rows = (await session.execute(stmt)).scalars().all()
        return any(a.covers(target_date) for a in rows)


async def get_active_absences(
    tenant_id: uuid.UUID, target_date: dt.date,
) -> list[tuple["Employee", EmployeeAbsence]]:
    """Alle Mitarbeiter eines Tenants die an target_date abwesend sind,
    plus ihr aktiver Absence-Eintrag.

    Returns: [(Employee, EmployeeAbsence), ...] sortiert: Default zuerst,
    dann nach Mitarbeiter-Slug.
    """
    from core.database import AsyncSessionLocal
    from core.models.employee import Employee
    async with AsyncSessionLocal() as session:
        stmt = (
            select(Employee, EmployeeAbsence)
            .join(EmployeeAbsence, EmployeeAbsence.employee_id == Employee.id)
            .where(
                EmployeeAbsence.tenant_id == tenant_id,
                EmployeeAbsence.start_date <= target_date,
            )
            .order_by(Employee.is_default.desc(), Employee.slug.asc())
        )
        rows = (await session.execute(stmt)).all()
        result: list[tuple[Employee, EmployeeAbsence]] = []
        for emp, absence in rows:
            if absence.covers(target_date):
                session.expunge(emp)
                session.expunge(absence)
                result.append((emp, absence))
        return result


async def get_upcoming_absences(
    tenant_id: uuid.UUID, days_ahead: int = 7,
) -> list[tuple["Employee", EmployeeAbsence]]:
    """Alle Absences die in den naechsten `days_ahead` Tagen ANGEHEN
    (also start_date > heute). Heutige Absences sind nicht dabei —
    die liefert get_active_absences().
    """
    from core.database import AsyncSessionLocal
    from core.models.employee import Employee
    today = dt.date.today()
    cutoff = today + dt.timedelta(days=days_ahead)
    async with AsyncSessionLocal() as session:
        stmt = (
            select(Employee, EmployeeAbsence)
            .join(EmployeeAbsence, EmployeeAbsence.employee_id == Employee.id)
            .where(
                EmployeeAbsence.tenant_id == tenant_id,
                EmployeeAbsence.start_date > today,
                EmployeeAbsence.start_date <= cutoff,
            )
            .order_by(EmployeeAbsence.start_date.asc(), Employee.slug.asc())
        )
        rows = (await session.execute(stmt)).all()
        out: list[tuple[Employee, EmployeeAbsence]] = []
        for emp, ab in rows:
            session.expunge(emp)
            session.expunge(ab)
            out.append((emp, ab))
        return out


async def create_absence(
    employee_id: uuid.UUID,
    start_date: dt.date,
    end_date: dt.date | None,
    absence_type: str,
    notes: str | None = None,
    created_by_employee_id: uuid.UUID | None = None,
) -> EmployeeAbsence:
    """Insert. tenant_id wird automatisch vom Employee uebernommen."""
    if absence_type not in ALLE_ABSENCE_TYPES:
        raise ValueError(
            f"absence_type {absence_type!r} ungueltig — erlaubt: "
            f"{ALLE_ABSENCE_TYPES}"
        )
    if end_date is not None and end_date < start_date:
        raise ValueError("end_date < start_date — bitte korrigieren.")

    from core.database import AsyncSessionLocal
    from core.models.employee import Employee
    async with AsyncSessionLocal() as session:
        emp = (await session.execute(
            select(Employee).where(Employee.id == employee_id)
        )).scalar_one_or_none()
        if emp is None:
            raise ValueError(f"Employee {employee_id} nicht gefunden.")
        ab = EmployeeAbsence(
            tenant_id=emp.tenant_id,
            employee_id=employee_id,
            start_date=start_date,
            end_date=end_date,
            absence_type=absence_type,
            notes=notes,
            created_by_employee_id=created_by_employee_id,
        )
        session.add(ab)
        await session.commit()
        await session.refresh(ab)
        session.expunge(ab)
        return ab


async def close_absence(
    employee_id: uuid.UUID, end_date: dt.date,
) -> EmployeeAbsence | None:
    """Setzt das end_date der aktiven (= heute oder zukuenftig
    laufenden) Absence des Mitarbeiters. Open-ended → end_date.
    Returns die aktualisierte Absence oder None wenn keine aktiv.

    Wird von /zurueck aufgerufen — User-Befehl "Max ist wieder gesund".
    """
    from core.database import AsyncSessionLocal
    today = dt.date.today()
    async with AsyncSessionLocal() as session:
        # Aktive Absence: start_date <= today UND (end_date IS NULL OR end_date >= today)
        stmt = (
            select(EmployeeAbsence)
            .where(
                EmployeeAbsence.employee_id == employee_id,
                EmployeeAbsence.start_date <= today,
            )
            .order_by(EmployeeAbsence.start_date.desc())
        )
        rows = (await session.execute(stmt)).scalars().all()
        active = None
        for a in rows:
            if a.end_date is None or a.end_date >= today:
                active = a
                break
        if active is None:
            return None
        active.end_date = end_date
        await session.commit()
        await session.refresh(active)
        session.expunge(active)
        return active


# ----------------------------------------------------------------------
# Verfuegbarkeits-Check: kombiniert is_active + arbeitstage +
# arbeitszeiten + Absence. Wird vom Skill-Router (target_datetime)
# und vom Cron benutzt.
# ----------------------------------------------------------------------


async def is_employee_working_at(
    employee_id: uuid.UUID, target: dt.datetime,
) -> bool:
    """True wenn der Mitarbeiter am `target`-Zeitpunkt arbeitet.

    Prueft in Reihenfolge:
    1. Employee existiert + is_active
    2. Keine Absence an target.date()
    3. target.weekday() in arbeitstage (Fallback Mo-Fr [0..4])
    4. target.time() in [arbeitszeiten.start, arbeitszeiten.end]
       (Fallback 08:00-17:00)

    Wenn arbeitszeiten/arbeitstage NULL: nutze konservativen
    Default Mo-Fr 8-17. (Tenant-spezifischer Default aus
    tool_configs.kalender koennte spaeter rein, ist hier nicht
    so wichtig — wenn ein Mitarbeiter aussergewoehnliche Zeiten
    hat, soll er sie pro Mitarbeiter setzen.)
    """
    from core.database import AsyncSessionLocal
    from core.models.employee import Employee
    async with AsyncSessionLocal() as session:
        emp = (await session.execute(
            select(Employee).where(Employee.id == employee_id)
        )).scalar_one_or_none()
    if emp is None or not emp.is_active:
        return False
    # Absence-Check
    if await is_employee_absent_on(employee_id, target.date()):
        return False
    # Arbeitstag
    workdays = emp.arbeitstage or [0, 1, 2, 3, 4]  # Mo-Fr default
    if target.weekday() not in workdays:
        return False
    # Arbeitszeit
    az = emp.arbeitszeiten or {"start": "08:00", "end": "17:00"}
    try:
        start = dt.time.fromisoformat(az.get("start", "08:00"))
        end = dt.time.fromisoformat(az.get("end", "17:00"))
    except (ValueError, AttributeError):
        start, end = dt.time(8, 0), dt.time(17, 0)
    t = target.time()
    return start <= t <= end


async def get_available_employees(
    tenant_id: uuid.UUID, target: dt.datetime,
) -> list["Employee"]:
    """Alle Mitarbeiter eines Tenants die am `target`-Zeitpunkt arbeiten.
    Vorgefilterte Liste fuer den Skill-Router.
    """
    from core.models.employee import get_employees_for_tenant
    all_emps = await get_employees_for_tenant(tenant_id, active_only=True)
    available: list = []
    for emp in all_emps:
        if await is_employee_working_at(emp.id, target):
            available.append(emp)
    return available
