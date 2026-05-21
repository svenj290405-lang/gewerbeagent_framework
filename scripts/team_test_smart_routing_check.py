"""Live-Beweis Smart-Routing: echte Gemini-Auswahl (kein Mock).

Legt kurz 2 Test-Mitarbeiter mit klaren Gewerken an (emil=elektrik,
klaus=sanitaer), schickt echte Anfragen durch choose_employee und zeigt,
wen Gemini waehlt. Raeumt die 2 Mitarbeiter danach wieder weg.
choose_employee mutiert nichts — nur die 2 Test-MA werden angelegt/geloescht.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import delete, select

from core.database import AsyncSessionLocal
from core.models import Tenant, Employee
from core.routing.employee_router import choose_employee

TENANT_SLUG = "pilot"
TEST = [("emil", "Emil Elektrik", ["elektrik"]),
        ("klaus", "Klaus Klempner", ["sanitaer"])]

ANFRAGEN = [
    "Die Steckdose in der Küche gibt keinen Strom mehr.",
    "Der Abfluss in der Dusche ist verstopft, alles steht voll Wasser.",
    "Mein Durchlauferhitzer macht keinen Strom und es kommt kein warmes Wasser.",
    "Bitte einmal die ganze Wohnung neu streichen.",  # keiner passt -> spannend
]


async def main():
    async with AsyncSessionLocal() as s:
        tenant = (await s.execute(
            select(Tenant).where(Tenant.slug == TENANT_SLUG)
        )).scalar_one()
        tid = tenant.id
        # Test-MA anlegen
        for slug, name, skills in TEST:
            if (await s.execute(select(Employee).where(
                Employee.tenant_id == tid, Employee.slug == slug,
            ))).scalar_one_or_none() is None:
                s.add(Employee(
                    tenant_id=tid, slug=slug, name=name, is_default=False,
                    is_active=True, skills=skills,
                    calendar_provider=None, calendar_id=None,
                    telegram_chat_id=None,
                ))
        await s.commit()

    print("=" * 72)
    print("Smart-Routing LIVE (echte Gemini-Calls) — Kandidaten: "
          "Sven(keine Skills) + emil(elektrik) + klaus(sanitaer)")
    print("=" * 72)
    try:
        for req in ANFRAGEN:
            dec = await choose_employee(tid, anliegen_text=req)
            if dec is None:
                print(f"\nAnfrage: {req}\n  → (keine Entscheidung)")
                continue
            tag = "🤖 GEMINI" if dec.reason == "gemini-skill-match" else \
                  f"⚙️ Fallback ({dec.reason})"
            print(f"\nAnfrage: {req}\n  → {dec.employee_slug}   [{tag}]")
    finally:
        async with AsyncSessionLocal() as s:
            await s.execute(delete(Employee).where(
                Employee.tenant_id == tid,
                Employee.slug.in_([t[0] for t in TEST]),
            ))
            await s.commit()
        print("\n(Test-Mitarbeiter emil/klaus wieder entfernt.)")
    print("=" * 72)


if __name__ == "__main__":
    asyncio.run(main())
