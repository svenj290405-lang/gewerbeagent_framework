"""Bucht EINEN Test-Termin fuer Sven Jantos (svenj05@gmx.de) auf dem
Microsoft-Kalender-Test-Betrieb und verifiziert direkt die neue
Namenssuche im find_events-Pfad.

Aufruf (im Container):
    uv run python -m scripts.book_test_termin_sven
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta

from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.models import OAuthToken, Tenant
from core.plugin_system import get_plugin_for_tenant
from core.plugin_system.registry import discover_plugins


async def main() -> int:
    # Plugin-Registry fuellen (passiert sonst nur im App-Startup, nicht
    # in einem Standalone-Skript).
    discover_plugins()

    # Test-Betrieb = Tenant mit Microsoft-Token (Default-Employee-Token zuerst)
    async with AsyncSessionLocal() as s:
        row = (await s.execute(
            select(Tenant, OAuthToken)
            .join(OAuthToken, OAuthToken.tenant_id == Tenant.id)
            .where(OAuthToken.provider == "microsoft")
            .order_by(OAuthToken.employee_id.is_(None).desc())
        )).first()
    if not row:
        print("FEHLER: Kein Microsoft-Token / Test-Betrieb gefunden")
        return 2
    tenant, token = row
    print(f"Betrieb:  {tenant.slug} ({tenant.company_name})")
    print(f"Postfach: {token.account_email}")

    kalender = await get_plugin_for_tenant(tenant.slug, "kalender")
    if kalender is None:
        print("FEHLER: Kein kalender-Plugin fuer diesen Betrieb")
        return 3

    # Naechste freie Slots ab morgen 09:00 holen
    anker = (datetime.now() + timedelta(days=1)).strftime("%d.%m.%Y")
    slot_res = await kalender.on_webhook(
        "find_free_slots", {"datum": anker, "uhrzeit": "09:00"},
    )
    slots = list(slot_res.get("slots") or [])
    if not slots:
        print(f"FEHLER: Keine freien Slots ab {anker} gefunden")
        return 4

    booked = None
    for sl in slots[:5]:
        payload = {
            "name": "Sven Jantos",
            "anliegen": "Testtermin Namenssuche",
            "datum": sl["datum"],
            "uhrzeit": sl["uhrzeit"],
            "kunde_email": "svenj05@gmx.de",
            "idempotency_key": f"sven-namenssuche-{datetime.now().isoformat()}",
        }
        # Dauer nur setzen wenn der Slot eine liefert — sonst nutzt
        # book_appointment die konfigurierte Default-Termindauer.
        if sl.get("dauer_minuten"):
            payload["dauer_minuten"] = sl["dauer_minuten"]
        res = await kalender.on_webhook("book_appointment", payload)
        if res.get("erfolg"):
            booked = (sl, res)
            break
        print(f"  Slot {sl['datum']} {sl['uhrzeit']} nicht gebucht: "
              f"{res.get('nachricht')}")

    if not booked:
        print("FEHLER: Keiner der freien Slots liess sich buchen")
        return 5
    sl, res = booked
    print(f"\nGEBUCHT: {sl['datum']} {sl['uhrzeit']} "
          f"-> event_id={res.get('event_id')}")

    # Verifikation: neue Namenssuche
    find_res = await kalender.on_webhook(
        "find_events", {"kunde_name": "Sven Jantos"},
    )
    print(f"\nNamenssuche find_events(kunde_name='Sven Jantos'): "
          f"erfolg={find_res.get('erfolg')} anzahl={find_res.get('anzahl')}")
    for t in find_res.get("termine", []):
        print(f"  - {t.get('start_dt')} | {t.get('summary')} | "
              f"name_match={t.get('kunde_name_match')} "
              f"src={t.get('match_source')}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
