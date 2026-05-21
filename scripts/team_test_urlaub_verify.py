"""ISOLIERTER Urlaub-Buchungssperre-Test — Kontrolle (read-only).

Liest Toms eingetragenen Urlaub und prüft die Buchungssperre über die
echten Funktionen (is_employee_working_at + choose_employee):
  - WÄHREND des Urlaubs: Tom arbeitet nicht & wird bei einer neuen
    Heizungs-Anfrage NICHT gebucht (jemand anderes übernimmt).
  - VOR/NACH dem Urlaub (nächster Werktag): Tom ist wieder buchbar.
Ändert nichts, bucht nichts.
"""
from __future__ import annotations

import asyncio
import datetime as dt

from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.models import Tenant, Employee, EmployeeAbsence
from core.models.employee_absence import ABSENCE_URLAUB, is_employee_working_at
from core.routing.employee_router import choose_employee

TENANT_SLUG = "pilot"
WD = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def _prev_weekday(d):
    x = d - dt.timedelta(days=1)
    while x.weekday() >= 5:
        x -= dt.timedelta(days=1)
    return x


def _next_weekday(d):
    x = d + dt.timedelta(days=1)
    while x.weekday() >= 5:
        x += dt.timedelta(days=1)
    return x


async def _row(tenant_id, tom_id, day):
    at_10 = dt.datetime.combine(day, dt.time(10, 0))
    working = await is_employee_working_at(tom_id, at_10)
    dec = await choose_employee(
        tenant_id, anliegen_text="Heizung kaputt, bitte Termin",
        target_datetime=at_10,
    )
    picked = dec.employee_slug if dec else "—"
    reason = dec.reason if dec else "—"
    tom_picked = bool(dec and dec.employee_id == tom_id)
    return working, picked, reason, tom_picked


async def main():
    print("=" * 72)
    print(f"Urlaub-Buchungssperre-Check — Tenant '{TENANT_SLUG}'")
    print("=" * 72)

    async with AsyncSessionLocal() as s:
        tenant = (await s.execute(
            select(Tenant).where(Tenant.slug == TENANT_SLUG)
        )).scalar_one()
        s.expunge(tenant)
        tom = (await s.execute(
            select(Employee).where(
                Employee.tenant_id == tenant.id, Employee.slug == "tom",
            )
        )).scalar_one_or_none()
        if tom is None:
            print("FEHLER: 'tom' nicht gefunden — erst team_test_urlaub_setup.py.")
            return
        s.expunge(tom)
        absence = (await s.execute(
            select(EmployeeAbsence).where(
                EmployeeAbsence.employee_id == tom.id,
                EmployeeAbsence.absence_type == ABSENCE_URLAUB,
            ).order_by(EmployeeAbsence.start_date.desc())
        )).scalars().first()

    if absence is None:
        print("Noch KEIN Urlaub für Tom eingetragen.")
        print("→ Bitte erst im Bot: /urlaub → Tom Test → 2026-05-25 → 2026-05-29")
        return

    start, end = absence.start_date, absence.end_date
    print(f"Toms Urlaub: {start} – {end or 'offen'}\n")

    # Testtage: Werktag davor, alle Werktage im Zeitraum, Werktag danach
    during = []
    if end is not None:
        d = start
        while d <= end:
            if d.weekday() < 5:
                during.append(d)
            d += dt.timedelta(days=1)
    else:
        during = [start + dt.timedelta(days=i) for i in range(0, 5)]
        during = [d for d in during if d.weekday() < 5]

    before = _prev_weekday(start)
    after = _next_weekday(end) if end is not None else None

    plan = [("VOR Urlaub", before, False)]
    plan += [("im Urlaub", d, True) for d in during]
    if after is not None:
        plan.append(("NACH Urlaub", after, False))

    print(f"{'Phase':12} {'Tag':14} {'arbeitet':9} {'gebucht→':12} {'Verdikt'}")
    print("-" * 72)
    all_ok = True
    for phase, day, in_urlaub in plan:
        working, picked, reason, tom_picked = await _row(tenant.id, tom.id, day)
        if in_urlaub:
            ok = (not working) and (not tom_picked)
            verdict = "✅ Tom gesperrt" if ok else "⚠️ FEHLER: Tom buchbar!"
        else:
            ok = working and tom_picked
            verdict = "✅ Tom buchbar" if ok else "⚠️ unerwartet"
        all_ok = all_ok and ok
        tag = f"{WD[day.weekday()]} {day.strftime('%d.%m.')}"
        print(f"{phase:12} {tag:14} {str(working):9} "
              f"{picked+'('+reason[:4]+')':12} {verdict}")

    print("-" * 72)
    print("GESAMT:", "✅ Buchungssperre greift korrekt über den ganzen Zeitraum"
          if all_ok else "⚠️ Mindestens ein Check fehlgeschlagen")
    print("\nHinweis: Urlaub verschiebt BESTEHENDE Termine NICHT (nur /krank tut das).")
    print("=" * 72)


if __name__ == "__main__":
    asyncio.run(main())
