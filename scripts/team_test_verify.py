"""ISOLIERTER Krank-E2E-Test — Kontrolle (read-only).

Zeigt nach dem /krank-Lauf: Inhalt von Max' + Annas Test-Kalender heute
und die aktiven Absences. Aendert nichts.
"""
from __future__ import annotations

import asyncio
import datetime as dt

from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.models import Tenant, Employee
from core.models.employee_absence import get_active_absences
from core.integrations.google_calendar import list_events_for_day

TENANT_SLUG = "pilot"


async def _show_cal(tenant_id, emp, today):
    if emp is None:
        print("  (Mitarbeiter nicht gefunden)")
        return
    evs = await list_events_for_day(
        tenant_id, today, employee_id=emp.id,
        calendar_id=emp.calendar_id or "primary",
    )
    print(f"  {emp.slug} ({emp.calendar_id[:20] if emp.calendar_id else 'primary'}…): "
          f"{len(evs)} Termin(e)")
    for e in evs:
        print(f"     {e['start_dt']:%H:%M}  {e['subject']}")


async def main():
    today = dt.date.today()
    print("=" * 72)
    print(f"Krank-E2E-Kontrolle — Tenant '{TENANT_SLUG}', {today}")
    print("=" * 72)

    async with AsyncSessionLocal() as s:
        tenant = (await s.execute(
            select(Tenant).where(Tenant.slug == TENANT_SLUG)
        )).scalar_one_or_none()
        if tenant is None:
            print("kein Tenant.")
            return
        s.expunge(tenant)
        emps = {
            e.slug: e for e in (await s.execute(
                select(Employee).where(
                    Employee.tenant_id == tenant.id,
                    Employee.slug.in_(["max", "anna"]),
                )
            )).scalars().all()
        }
        for e in emps.values():
            s.expunge(e)

    print("\nKalender-Inhalt heute:")
    await _show_cal(tenant.id, emps.get("max"), today)
    await _show_cal(tenant.id, emps.get("anna"), today)

    print("\nAktive Absences heute:")
    active = await get_active_absences(tenant.id, today)
    if not active:
        print("  (keine)")
    for emp, ab in active:
        print(f"  🤒 {emp.slug}: {ab.absence_type} {ab.start_date}–{ab.end_date or 'offen'}")

    print("\nErwartung nach /krank Max:")
    print("  max  → 0 Termine (alle weg)")
    print("  anna → 2 Termine (10:00 + 14:00)")
    print("  19:00 bleibt nirgends (no-coverage → an Inhaber eskaliert)")
    print("=" * 72)


if __name__ == "__main__":
    asyncio.run(main())
