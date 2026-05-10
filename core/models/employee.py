"""Employee — ein Mitarbeiter eines Tenants.

Bisher war im Framework "1 Tenant == 1 Person" hartcodiert: Tenant
hatte EINEN telegram_chat_id, EINE Werkstatt-Heimat, EINEN Google-
OAuth-Token. Mit Multi-Mitarbeiter-Setup (Plan: das-machen-wir-gleich-
foamy-frost.md, Phase 0) bekommt jeder Tenant 1..N Employees, jeder
mit eigener Telegram-Identitaet, eigener Heimat-Adresse, eigenem
Skill-Set und (Phase 1) eigenem OAuth-Token.

Backward-Compatibility:
- Migration legt fuer jeden bestehenden Tenant exakt einen Default-
  Employee an (is_default=true), der die heutigen Tenant-Felder erbt.
  Nicht "Sonderfall: kein Employee", sondern "1-Person-Tenant hat 1
  Employee" — der Code bleibt employee-zentrisch ohne if-else.
- tenants.telegram_chat_id, tenants.heimat_* bleiben als "Mirror" des
  Default-Employee bestehen, damit aeltere Code-Pfade weiter lesen
  koennen. Wird ueber mehrere Phasen migriert, dann eventuell gedroppt.

Felder fuer alle Phasen sind direkt vorgesehen, damit keine zweite
Migration noetig wird wenn Phase 2/3/4 implementiert werden:
- Phase 2: telegram_chat_id (Multi-Telegram pro Tenant)
- Phase 3: heimat_* (Per-Mitarbeiter-Smart-Routing)
- Phase 4: skills, arbeitszeiten, arbeitstage (Skill-Routing + per-User
  Schichten — arbeitszeiten/-tage erst in Phase 4 aktiv genutzt; bis
  dahin null = Tenant-Default aus tool_configs.kalender)
"""
from __future__ import annotations

import decimal
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database.base import Base

if TYPE_CHECKING:
    from core.models.tenant import Tenant


# --- Skill-Konstanten ---
# Vordefinierte Skill-Strings die der Skill-Router (Phase 5) per
# Keyword-Map auf Anliegen-Texte matcht. Tenants koennen freie Skill-
# Strings in employees.skills setzen — die Konstanten sind nur die
# verbreitetesten. Eine eigene tenant_skills-Tabelle waere Phase-2-
# Overkill.
SKILL_HEIZUNG = "heizung"
SKILL_SANITAER = "sanitaer"
SKILL_ELEKTRIK = "elektrik"
SKILL_DACH = "dach"
SKILL_TISCHLER = "tischler"
SKILL_MALER = "maler"
SKILL_ALLGEMEIN = "allgemein"

ALLE_SKILLS = (
    SKILL_HEIZUNG, SKILL_SANITAER, SKILL_ELEKTRIK,
    SKILL_DACH, SKILL_TISCHLER, SKILL_MALER, SKILL_ALLGEMEIN,
)


class Employee(Base):
    """Ein Mitarbeiter eines Tenants.

    Pro Tenant gibt es genau einen Employee mit is_default=true (durch
    partial unique index erzwungen) — das ist der "Inhaber" oder
    Onboarding-Account. Weitere Employees werden ueber den
    /mitarbeiter-Wizard (Phase 4) angelegt.
    """

    __tablename__ = "employees"

    __table_args__ = (
        UniqueConstraint("tenant_id", "slug", name="uq_emp_tenant_slug"),
        Index("ix_emp_tenant_default", "tenant_id", "is_default"),
        Index(
            "uq_emp_default_per_tenant",
            "tenant_id",
            unique=True,
            postgresql_where=text("is_default"),
        ),
        Index("ix_emp_chat", "telegram_chat_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Identitaet
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    contact_email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true", default=True,
    )
    notes: Mapped[str | None] = mapped_column(String(2000), nullable=True)

    # Phase 2 — Telegram-Identitaet
    telegram_chat_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, unique=True,
    )

    # Phase 3 — Werkstatt-Heimat fuer Smart-Routing
    heimat_strasse: Mapped[str | None] = mapped_column(String(255), nullable=True)
    heimat_plz: Mapped[str | None] = mapped_column(String(10), nullable=True)
    heimat_ort: Mapped[str | None] = mapped_column(String(200), nullable=True)
    heimat_lat: Mapped[decimal.Decimal | None] = mapped_column(
        Numeric(precision=9, scale=6), nullable=True,
    )
    heimat_lon: Mapped[decimal.Decimal | None] = mapped_column(
        Numeric(precision=9, scale=6), nullable=True,
    )
    fahrtzeit_puffer_min: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="15", default=15,
    )

    # Phase 4 — Skills + Arbeitszeiten
    # skills: ARRAY von Strings, frei gewaehlt aus ALLE_SKILLS oder
    # tenant-spezifisch. Phase-5-Router matcht per substring/exact.
    skills: Mapped[list[str] | None] = mapped_column(ARRAY(String(50)), nullable=True)
    # arbeitszeiten/-tage: NULL = Tenant-Default aus
    # tool_configs.kalender.arbeitszeiten_start/_ende/arbeitstage.
    # Erst wenn Mitarbeiter abweichende Schicht hat, hier setzen.
    arbeitszeiten: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    arbeitstage: Mapped[list[int] | None] = mapped_column(ARRAY(Integer), nullable=True)

    # --- Relationships ---
    tenant: Mapped["Tenant"] = relationship(lazy="joined")

    def __repr__(self) -> str:
        flag = " [default]" if self.is_default else ""
        active = "" if self.is_active else " [inactive]"
        return f"<Employee {self.slug}@{self.tenant_id}{flag}{active}>"


# ----------------------------------------------------------------------
# Helper-Funktionen — von allen Phasen 1..5 verwendet.
# ----------------------------------------------------------------------


async def get_default_employee(tenant_id: uuid.UUID) -> Employee | None:
    """Liefert den Default-Employee eines Tenants.

    Garantiert eindeutig durch partial unique index. Liefert None
    nur wenn der Tenant noch nie durch die Phase-0-Backfill-Migration
    gelaufen ist (sollte in der Praxis nicht vorkommen).
    """
    from core.database import AsyncSessionLocal
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Employee).where(
                Employee.tenant_id == tenant_id,
                Employee.is_default.is_(True),
            )
        )
        return result.scalar_one_or_none()


async def get_employees_for_tenant(
    tenant_id: uuid.UUID,
    *,
    active_only: bool = True,
) -> list[Employee]:
    """Liefert alle Employees eines Tenants, sortiert: Default zuerst, dann Slug ASC."""
    from core.database import AsyncSessionLocal
    async with AsyncSessionLocal() as session:
        stmt = select(Employee).where(Employee.tenant_id == tenant_id)
        if active_only:
            stmt = stmt.where(Employee.is_active.is_(True))
        stmt = stmt.order_by(Employee.is_default.desc(), Employee.slug.asc())
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def get_employee_by_telegram_chat(
    chat_id: int,
) -> tuple["Tenant", Employee] | None:
    """Findet (Tenant, Employee) anhand einer Telegram-Chat-ID.

    Sucht zuerst employees.telegram_chat_id (= Multi-User), faellt
    zurueck auf tenants.telegram_chat_id (= Legacy / Default-Employee).
    Im Fallback-Fall wird der Default-Employee als Employee
    zurueckgegeben.

    Die zurueckgegebenen Objekte sind aus der Session expunged —
    der Caller kann sie ohne aktive Session weiterverwenden, darf
    aber keine relationships lazy-loaden.
    """
    from core.database import AsyncSessionLocal
    from core.models.tenant import Tenant
    async with AsyncSessionLocal() as session:
        # 1) Direkter Match auf employees.telegram_chat_id
        emp = (await session.execute(
            select(Employee).where(Employee.telegram_chat_id == chat_id)
        )).scalar_one_or_none()
        if emp is not None:
            tenant = (await session.execute(
                select(Tenant).where(Tenant.id == emp.tenant_id)
            )).scalar_one()
            session.expunge(emp)
            session.expunge(tenant)
            return tenant, emp
        # 2) Fallback: alter tenants.telegram_chat_id-Pfad
        tenant = (await session.execute(
            select(Tenant).where(Tenant.telegram_chat_id == chat_id)
        )).scalar_one_or_none()
        if tenant is None:
            return None
        emp = (await session.execute(
            select(Employee).where(
                Employee.tenant_id == tenant.id,
                Employee.is_default.is_(True),
            )
        )).scalar_one_or_none()
        if emp is None:
            # Tenant existiert, aber kein Default-Employee — sollte
            # nicht vorkommen wenn Migration sauber lief.
            return None
        session.expunge(emp)
        session.expunge(tenant)
        return tenant, emp
