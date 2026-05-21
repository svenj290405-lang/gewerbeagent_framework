"""ISOLIERTER Urlaub-Buchungssperre-Test — Setup.

Legt EINEN Test-Mitarbeiter `tom` (Skills heizung+sanitaer) im pilot-
Tenant an. Kein Kalender noetig — die Buchungssperre ist reine Routing-
Logik (choose_employee + is_employee_working_at). Danach setzt DU den
Urlaub live im Bot, anschliessend verifiziert team_test_urlaub_verify.py.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.models import Tenant, Employee

TENANT_SLUG = "pilot"


async def main():
    print("=" * 72)
    print(f"Urlaub-Test-Setup (isoliert) — Tenant '{TENANT_SLUG}'")
    print("=" * 72)

    async with AsyncSessionLocal() as s:
        tenant = (await s.execute(
            select(Tenant).where(Tenant.slug == TENANT_SLUG)
        )).scalar_one_or_none()
        if tenant is None:
            print(f"FEHLER: kein Tenant '{TENANT_SLUG}'.")
            return
        exists = (await s.execute(
            select(Employee).where(
                Employee.tenant_id == tenant.id, Employee.slug == "tom",
            )
        )).scalar_one_or_none()
        if exists is not None:
            print("'tom' existiert schon — erst team_test_teardown.py laufen lassen.")
            return
        tom = Employee(
            tenant_id=tenant.id, slug="tom", name="Tom Test",
            is_default=False, is_active=True,
            skills=["heizung", "sanitaer"],
            calendar_provider=None, calendar_id=None,
            telegram_chat_id=None,
        )
        s.add(tom)
        await s.commit()
        await s.refresh(tom)
        print(f"Mitarbeiter angelegt: tom -> {tom.id} (skills {tom.skills})")

    print("\n" + "=" * 72)
    print("Jetzt im Telegram-Bot Urlaub setzen:")
    print("  1) /urlaub")
    print("  2) 'Tom Test' wählen")
    print("  3) Start (ab wann?):  2026-05-25")
    print("  4) Ende  (bis wann?): 2026-05-29")
    print("  → Bestätigung: 'Tom Test Urlaub 25.05.2026–29.05.2026 eingetragen'")
    print("\nDanach prüfen: scripts/team_test_urlaub_verify.py")
    print("Aufräumen:     scripts/team_test_teardown.py")
    print("=" * 72)


if __name__ == "__main__":
    asyncio.run(main())
