"""Leichtgewichtiges Nutzungs-Event der PWA — fuer Aktivitaets-Tracking.

Eine Zeile pro relevanter Nutzer-Aktion (Login, Assistent-Befehl,
Assistent-Aktion, Diktat). Daraus aggregiert das Admin-Dashboard die
Betriebs-Aktivitaet und der App-Team-Screen die Pro-Mitarbeiter-Aktivitaet.

Bewusst KEIN Eintrag in ``api_usage_log`` (das ist fuer Kosten/Abrechnung):
hier geht es um WER die App wie viel nutzt, mit ``employee_id``-Bezug.
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base

logger = logging.getLogger(__name__)

# Event-Arten
USAGE_LOGIN = "login"
USAGE_ASSISTENT_BEFEHL = "assistent_befehl"
USAGE_ASSISTENT_AKTION = "assistent_aktion"
USAGE_DIKTAT = "diktat"

USAGE_KINDS = {
    USAGE_LOGIN, USAGE_ASSISTENT_BEFEHL, USAGE_ASSISTENT_AKTION, USAGE_DIKTAT,
}


class AppUsageEvent(Base):
    """Ein Nutzungs-Event eines Mitarbeiters in der PWA."""

    __tablename__ = "app_usage_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    # employee_id nullable + SET NULL: Events ueberleben das Loeschen eines
    # Mitarbeiters (Betriebs-Statistik bleibt korrekt).
    employee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("employees.id", ondelete="SET NULL"),
        nullable=True,
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)

    __table_args__ = (
        Index("ix_app_usage_tenant_kind_created", "tenant_id", "kind", "created_at"),
        Index("ix_app_usage_tenant_emp", "tenant_id", "employee_id"),
    )


async def record_app_usage(
    tenant_id: uuid.UUID | None,
    employee_id: uuid.UUID | None,
    kind: str,
) -> None:
    """Schreibt EIN Nutzungs-Event. **Failsafe** — Tracking darf den Request
    niemals brechen: jeder Fehler (DB weg, ungueltige IDs) wird geschluckt.
    Eigene Session, damit ein Fehler die Transaktion des Aufrufers nicht
    beeinflusst."""
    if not tenant_id or kind not in USAGE_KINDS:
        return
    try:
        from core.database import AsyncSessionLocal
        async with AsyncSessionLocal() as s:
            s.add(AppUsageEvent(
                tenant_id=tenant_id, employee_id=employee_id, kind=kind))
            await s.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("record_app_usage(%s) fehlgeschlagen: %s", kind, exc)


async def usage_counts_by_employee(
    tenant_id: uuid.UUID, *, since: dt.datetime,
) -> dict[str, dict[str, int]]:
    """Pro Mitarbeiter (employee_id als str) die Event-Zahlen seit ``since``,
    aufgeschluesselt nach kind. Form: {emp_id: {kind: count, ...}}."""
    from core.database import AsyncSessionLocal
    from sqlalchemy import select, func
    out: dict[str, dict[str, int]] = {}
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(
            select(AppUsageEvent.employee_id, AppUsageEvent.kind, func.count(AppUsageEvent.id))
            .where(AppUsageEvent.tenant_id == tenant_id)
            .where(AppUsageEvent.employee_id.is_not(None))
            .where(AppUsageEvent.created_at >= since)
            .group_by(AppUsageEvent.employee_id, AppUsageEvent.kind)
        )).all()
    for emp_id, kind, cnt in rows:
        out.setdefault(str(emp_id), {})[kind] = int(cnt)
    return out
