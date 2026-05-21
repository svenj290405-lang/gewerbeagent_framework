"""ISOLIERTER Krank-E2E-Test — Setup.

Legt im pilot-Tenant zwei TEST-Mitarbeiter an, jeder mit einem EIGENEN
neuen Google-Kalender (im Konto des Inhabers, ueber dessen OAuth-Token).
Der echte Primaer-Kalender des Inhabers wird NIE angefasst.

  - max  (wird gleich krankgemeldet) -> Test-Kalender A
  - anna (Ersatz, Skills heizung+sanitaer) -> Test-Kalender B

Plus 3 Test-Termine HEUTE in Max' Kalender und eine read-only
Vorhersage, wohin jeder Termin bei Krankmeldung gehen wird.

Danach: im Bot `/krank` -> Max -> "Nur heute".
Aufraeumen: scripts/team_test_teardown.py
"""
from __future__ import annotations

import asyncio
import datetime as dt

import httpx
from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.models import Tenant, Employee
from core.models.employee import get_default_employee
from core.integrations.google_calendar import (
    GOOGLE_CAL_BASE, _ensure_fresh_access_token, create_event,
)
from core.security.oauth_token_lookup import find_oauth_token
from core.routing.employee_router import choose_employee

TENANT_SLUG = "pilot"
CAL_MAX_NAME = "🧪 Test Max (Krank-Demo)"
CAL_ANNA_NAME = "🧪 Test Anna (Krank-Demo)"
DEMO_TAG = "KRANK-DEMO-TEST"  # in der Description jedes Test-Events


async def _create_secondary_calendar(access_token: str, summary: str) -> str:
    """Legt einen neuen sekundaeren Google-Kalender an, gibt dessen id."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{GOOGLE_CAL_BASE}/calendars",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"summary": summary, "timeZone": "Europe/Berlin"},
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"Kalender-Anlage fehlgeschlagen {resp.status_code}: "
                f"{resp.text[:300]}"
            )
        return resp.json()["id"]


async def _upsert_employee(tenant_id, *, slug, name, skills, calendar_id):
    async with AsyncSessionLocal() as s:
        emp = (await s.execute(
            select(Employee).where(
                Employee.tenant_id == tenant_id, Employee.slug == slug,
            )
        )).scalar_one_or_none()
        if emp is not None:
            raise RuntimeError(
                f"Mitarbeiter '{slug}' existiert schon — erst "
                f"scripts/team_test_teardown.py laufen lassen."
            )
        emp = Employee(
            tenant_id=tenant_id, slug=slug, name=name,
            is_default=False, is_active=True, skills=skills,
            calendar_provider="google", calendar_id=calendar_id,
            telegram_chat_id=None,
        )
        s.add(emp)
        await s.commit()
        await s.refresh(emp)
        s.expunge(emp)
        return emp


async def main():
    today = dt.date.today()
    print("=" * 72)
    print(f"Krank-E2E-Setup (isoliert) — Tenant '{TENANT_SLUG}', Datum {today} "
          f"({['Mo','Di','Mi','Do','Fr','Sa','So'][today.weekday()]})")
    print("=" * 72)

    async with AsyncSessionLocal() as s:
        tenant = (await s.execute(
            select(Tenant).where(Tenant.slug == TENANT_SLUG)
        )).scalar_one_or_none()
        if tenant is None:
            print(f"FEHLER: kein Tenant '{TENANT_SLUG}'.")
            return
        s.expunge(tenant)

    inhaber = await get_default_employee(tenant.id)
    print(f"Inhaber (Token-Quelle): {inhaber.name} / {inhaber.slug}")

    # OAuth-Token des Inhabers -> Access-Token (fuer Kalender-Anlage)
    token = await find_oauth_token(tenant.id, "google", inhaber.id)
    if token is None:
        print("FEHLER: kein Google-OAuth-Token fuer den Inhaber gefunden.")
        return
    access = await _ensure_fresh_access_token(token)

    # 1) Zwei neue Test-Kalender im Google-Konto des Inhabers
    print("\n[1] Lege Test-Kalender an …")
    cal_max = await _create_secondary_calendar(access, CAL_MAX_NAME)
    cal_anna = await _create_secondary_calendar(access, CAL_ANNA_NAME)
    print(f"    Max-Kalender:  {cal_max}")
    print(f"    Anna-Kalender: {cal_anna}")

    # 2) Zwei Test-Mitarbeiter
    print("\n[2] Lege Test-Mitarbeiter an …")
    max_e = await _upsert_employee(
        tenant.id, slug="max", name="Max Test",
        skills=[], calendar_id=cal_max,
    )
    anna_e = await _upsert_employee(
        tenant.id, slug="anna", name="Anna Test",
        skills=["heizung", "sanitaer"], calendar_id=cal_anna,
    )
    print(f"    max  -> {max_e.id} (skills: {max_e.skills})")
    print(f"    anna -> {anna_e.id} (skills: {anna_e.skills})")

    # 3) Drei Test-Termine HEUTE in Max' Kalender
    print("\n[3] Lege 3 Test-Termine heute in Max' Kalender an …")
    events_spec = [
        (dt.time(10, 0), "Heizung Müller – Kessel tropft", "Hauptstr. 5"),
        (dt.time(14, 0), "Wasserhahn tropft – Bad Schmidt", "Lindenweg 12"),
        (dt.time(19, 0), "Abend-Notdienst (nach Feierabend)", "Ringstr. 3"),
    ]
    created = []
    for t, subject, ort in events_spec:
        start = dt.datetime.combine(today, t)
        end = start + dt.timedelta(hours=1)
        res = await create_event(
            tenant.id, summary=subject,
            description=f"{DEMO_TAG} — Test-Termin",
            location=ort, start=start, end=end,
            employee_id=max_e.id, calendar_id=cal_max,
        )
        created.append((start, subject, ort, res.get("id")))
        print(f"    {start:%H:%M} {subject}  (id={res.get('id')[:18]}…)")

    # 4) READ-ONLY Vorhersage: wohin geht jeder Termin bei /krank Max?
    print("\n[4] Routing-Vorhersage (read-only, nichts wird verschoben):")
    print("    Krank: max  | Kandidaten: Inhaber + anna")
    for start, subject, ort, _id in created:
        decision = await choose_employee(
            tenant.id, anliegen_text=subject, kunde_adresse=ort,
            target_datetime=start, exclude_employee_ids=[max_e.id],
        )
        if decision is None:
            verdict = "— (keine Entscheidung)"
        elif decision.reason == "no-coverage":
            verdict = "⚠️  no-coverage → Eskalation an Inhaber"
        else:
            verdict = (f"→ {decision.employee_slug}  "
                       f"(reason={decision.reason}, score={decision.score})")
        print(f"    {start:%H:%M} {subject[:34]:34} {verdict}")

    print("\n" + "=" * 72)
    print("Setup fertig. Jetzt im Telegram-Bot:")
    print("  1) /team      → Max + Anna sollten erscheinen")
    print("  2) /krank     → 'Max Test' wählen → 'Nur heute'")
    print("  3) Push + Zusammenfassung beobachten")
    print("Danach: scripts/team_test_verify.py  (Kontrolle)")
    print("Aufräumen: scripts/team_test_teardown.py")
    print("=" * 72)


if __name__ == "__main__":
    asyncio.run(main())
