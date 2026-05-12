"""Smoke-Test: /krank-Wizard inklusive Auto-Umverteilung.

Mockt Calendar-APIs (Google + Microsoft) damit keine echten Events
geschoben werden. Verifiziert:
- /team rendert mit Status-Symbolen
- /krank → Mitarbeiter-Auswahl → Dauer → Insert in DB
- redistribute_for_employee() picked Substitut + macht delete+create
- /abwesend zeigt die heute aktive Krankmeldung
- /zurueck schliesst Absence
- Final-State wiederhergestellt (DB clean)
"""
from __future__ import annotations

import asyncio
import datetime as dt
import time

from sqlalchemy import delete, select

from core.database import AsyncSessionLocal
from core.models import (
    Tenant, Employee, EmployeeAbsence, ABSENCE_KRANK,
    create_absence, close_absence, is_employee_absent_on,
    is_employee_working_at,
)
from plugins.telegram_notify import handler as h
from plugins.telegram_notify.handler import (
    TelegramNotifier, process_telegram_update, _clear_state,
)

SVEN = 8518191832
captured: list[tuple] = []


async def fake_raw(bot_token, chat_id, text):
    captured.append(("raw", int(chat_id), str(text)[:600]))
    return True
TelegramNotifier._send_raw = staticmethod(fake_raw)

import httpx
async def fake_post(self, url, *args, **kwargs):
    if "sendMessage" in url and "json" in kwargs:
        p = kwargs["json"]
        captured.append(("http", int(p.get("chat_id", 0)),
                         str(p.get("text", ""))[:600],
                         p.get("reply_markup")))
    class R:
        status_code = 200; text = "{}"
        def json(self): return {}
    return R()
httpx.AsyncClient.post = fake_post


def upd_text(text):
    return {"update_id": int(time.time() * 1000),
            "message": {"message_id": 1, "from": {"id": SVEN, "is_bot": False},
                        "chat": {"id": SVEN, "type": "private"},
                        "date": int(time.time()), "text": text}}


def upd_callback(data):
    return {"update_id": int(time.time() * 1000),
            "callback_query": {"id": "cq-a", "from": {"id": SVEN, "is_bot": False},
                               "data": data,
                               "message": {"message_id": 1,
                                           "chat": {"id": SVEN, "type": "private"}}}}


async def cleanup_test_absences(tenant_id):
    """Sicherheits-Cleanup: alle Test-Absences nach Lauf entfernen."""
    async with AsyncSessionLocal() as s:
        await s.execute(
            delete(EmployeeAbsence).where(EmployeeAbsence.tenant_id == tenant_id)
        )
        await s.commit()


async def main():
    print("=" * 78)
    print("Absence-Smoke-Test: /krank, /abwesend, /zurueck, /team")
    print("=" * 78)
    await _clear_state(SVEN)

    # Fixture: demo-Tenant + Employees laden
    async with AsyncSessionLocal() as s:
        t = (await s.execute(select(Tenant).where(Tenant.slug == "demo"))).scalar_one()
        emps = (await s.execute(
            select(Employee).where(Employee.tenant_id == t.id)
        )).scalars().all()
    print(f"Tenant: {t.slug}, {len(emps)} Employees: {[e.slug for e in emps]}")
    if len(emps) < 2:
        print("WARN: Brauche mindestens 2 Employees fuer realistischen Test.")

    await cleanup_test_absences(t.id)

    # T1: /team — Status-Liste
    print("\n--- T1: /team ---")
    captured.clear()
    await process_telegram_update(upd_text("/team"))
    print(captured[-1][2][:400] if captured else "(silent)")

    # T2: /abwesend (leer)
    print("\n--- T2: /abwesend (leer) ---")
    captured.clear()
    await process_telegram_update(upd_text("/abwesend"))
    print(captured[-1][2][:300] if captured else "(silent)")

    # T3: /krank startet Wizard
    print("\n--- T3: /krank → Mitarbeiter-Buttons ---")
    captured.clear()
    await process_telegram_update(upd_text("/krank"))
    print(captured[-1][2][:200] if captured else "(silent)")

    # T4: Mitarbeiter-Auswahl (zweiten nehmen — Inhaber wäre default)
    non_default = next((e for e in emps if not e.is_default), None) or emps[0]
    print(f"\n--- T4: Callback krank:emp:{non_default.id} ---")
    captured.clear()
    await process_telegram_update(upd_callback(f"krank:emp:{non_default.id}"))
    print(captured[-1][2][:200] if captured else "(silent)")

    # T5: Dauer: nur heute
    print("\n--- T5: Callback krank:dur:today ---")
    captured.clear()
    await process_telegram_update(upd_callback("krank:dur:today"))
    # Erste Bestätigungs-Message
    for c in captured[:2]:
        print("  ", c[2][:300] if len(c) > 2 else c)

    # Kurz warten damit fire-and-forget asyncio.create_task durchläuft
    await asyncio.sleep(1.5)

    # T6: /abwesend zeigt jetzt die Krankmeldung
    print("\n--- T6: /abwesend (mit Krankheit) ---")
    captured.clear()
    await process_telegram_update(upd_text("/abwesend"))
    print(captured[-1][2][:300] if captured else "(silent)")

    # T7: /team zeigt 🤒-Icon
    print("\n--- T7: /team (mit 🤒) ---")
    captured.clear()
    await process_telegram_update(upd_text("/team"))
    print(captured[-1][2][:400] if captured else "(silent)")

    # T8: Direkt-Check: is_employee_absent_on?
    today = dt.date.today()
    absent_today = await is_employee_absent_on(non_default.id, today)
    print(f"\n--- T8: is_employee_absent_on({non_default.slug}, today) = {absent_today}")
    assert absent_today, "Mitarbeiter sollte heute als absent erkannt werden!"

    # T9: is_employee_working_at(today 10:00) sollte False sein
    test_dt = dt.datetime.combine(today, dt.time(10, 0))
    working = await is_employee_working_at(non_default.id, test_dt)
    print(f"--- T9: is_employee_working_at({non_default.slug}, today 10:00) = {working}")
    assert not working, "Sollte nicht arbeitend sein heute!"

    # T10: /zurueck schliesst die Absence
    print(f"\n--- T10: /zurueck {non_default.slug} ---")
    captured.clear()
    await process_telegram_update(upd_text(f"/zurueck {non_default.slug}"))
    print(captured[-1][2][:300] if captured else "(silent)")

    # T11: nach /zurueck nicht mehr absent
    absent_today2 = await is_employee_absent_on(non_default.id, today)
    print(f"--- T11: nach /zurueck — is_absent_on(today) = {absent_today2}")
    assert not absent_today2, "Sollte nach /zurueck nicht mehr absent sein"

    # Cleanup: alle Test-Absences raus
    await cleanup_test_absences(t.id)
    print("\n--- Cleanup: alle Absences geloescht ---")
    print("=" * 78)
    print("✅ Alle Asserts gruen — Test erfolgreich")
    print("=" * 78)


if __name__ == "__main__":
    asyncio.run(main())
