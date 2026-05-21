"""READ-ONLY Krank-Logik-Check (ändert nichts, sendet nichts).

Verifiziert über die echten Funktionen:
  - Absence ist gesetzt (get_active_absences / is_employee_absent_on)
  - Max arbeitet heute NICHT (krank), Anna schon
  - Buchungssperre: eine NEUE Anfrage heute wird NICHT an Max geroutet
    (choose_employee filtert den Kranken raus)
"""
from __future__ import annotations

import asyncio
import datetime as dt

from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.models import Tenant, Employee
from core.models.employee_absence import (
    get_active_absences, is_employee_absent_on, is_employee_working_at,
)
from core.routing.employee_router import choose_employee

TENANT_SLUG = "pilot"


async def main():
    today = dt.date.today()
    at_10 = dt.datetime.combine(today, dt.time(10, 0))
    print("=" * 72)
    print(f"Krank-Logik-Check — {TENANT_SLUG}, {today}")
    print("=" * 72)

    async with AsyncSessionLocal() as s:
        tenant = (await s.execute(
            select(Tenant).where(Tenant.slug == TENANT_SLUG)
        )).scalar_one()
        s.expunge(tenant)
        emps = {
            e.slug: e for e in (await s.execute(
                select(Employee).where(Employee.tenant_id == tenant.id)
            )).scalars().all()
        }
        for e in emps.values():
            s.expunge(e)

    print("\n[Absence-Status heute]")
    active = await get_active_absences(tenant.id, today)
    for emp, ab in active:
        print(f"  🤒 {emp.slug}: {ab.absence_type} {ab.start_date}–{ab.end_date or 'offen'}")
    if not active:
        print("  (keine)")

    max_e, anna_e = emps.get("max"), emps.get("anna")
    print("\n[Verfügbarkeit heute 10:00]")
    for label, e in (("max (krank)", max_e), ("anna (Ersatz)", anna_e)):
        if e is None:
            print(f"  {label}: (fehlt)")
            continue
        absent = await is_employee_absent_on(e.id, today)
        working = await is_employee_working_at(e.id, at_10)
        print(f"  {label}: absent={absent}  arbeitet_10:00={working}")

    print("\n[Buchungssperre: NEUE Heizungs-Anfrage heute 10:00 → wer?]")
    dec = await choose_employee(
        tenant.id, anliegen_text="Heizung kaputt, bitte Termin",
        target_datetime=at_10,
    )
    if dec is None:
        print("  → keine Entscheidung")
    else:
        gewaehlt = dec.employee_slug
        ist_max = (max_e is not None and dec.employee_id == max_e.id)
        print(f"  → {gewaehlt} (reason={dec.reason})")
        print(f"  Max wird gebucht? {'JA ⚠️ FEHLER' if ist_max else 'NEIN ✅ (Sperre greift)'}")

    print("\n[Annas Kalender (verschobene Termine)]")
    if anna_e:
        from core.integrations.google_calendar import list_events_for_day
        evs = await list_events_for_day(
            tenant.id, today, employee_id=anna_e.id,
            calendar_id=anna_e.calendar_id or "primary",
        )
        print(f"  {len(evs)} Termin(e):")
        for ev in evs:
            print(f"     {ev['start_dt']:%H:%M}  {ev['subject']}")
    print("=" * 72)


if __name__ == "__main__":
    asyncio.run(main())
