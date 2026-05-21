"""Bereitet einen manuellen /storno-Chat-Test vor.

Stellt sicher, dass es im Kalender des Test-Betriebs einen Termin fuer
Sven Jantos (svenj05@gmx.de) gibt, der eine VERKNUEPFTE EmailConversation
hat — damit ein manuelles /storno im Telegram-Chat die Bestaetigungs-Mail
an svenj05@gmx.de ausloest. Storniert NICHTS und schickt KEINE Mail.

Aufruf (im Container):
    uv run python -m scripts.setup_storno_chat_test_sven
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

KUNDE_EMAIL = "svenj05@gmx.de"
KUNDE_NAME = "Sven Jantos"


async def _ensure_conversation(tenant_id, event_id):
    from core.integrations.mail_pipeline import (
        find_conversation_by_event_id, create_conversation,
    )
    conv = await find_conversation_by_event_id(tenant_id, event_id)
    if conv and conv.kunde_email:
        return conv, False
    conv = await create_conversation(
        tenant_id, KUNDE_EMAIL, KUNDE_NAME, "Ihre Terminbuchung",
        gcal_event_id=event_id,
    )
    return conv, True


async def main() -> int:
    discover_plugins()
    async with AsyncSessionLocal() as s:
        row = (await s.execute(
            select(Tenant, OAuthToken)
            .join(OAuthToken, OAuthToken.tenant_id == Tenant.id)
            .where(OAuthToken.provider == "microsoft")
            .order_by(OAuthToken.employee_id.is_(None).desc())
        )).first()
    if not row:
        print("FEHLER: Kein Microsoft-Test-Betrieb")
        return 2
    tenant, token = row
    print(f"Betrieb: {tenant.slug} ({tenant.company_name})  Postfach: {token.account_email}")
    kalender = await get_plugin_for_tenant(tenant.slug, "kalender")
    if kalender is None:
        print("FEHLER: Kein kalender-Plugin")
        return 3

    find_res = await kalender.on_webhook("find_events", {"kunde_name": KUNDE_NAME})
    termine = find_res.get("termine", [])

    if not termine:
        anker = (datetime.now() + timedelta(days=3)).strftime("%d.%m.%Y")
        slot_res = await kalender.on_webhook(
            "find_free_slots", {"datum": anker, "uhrzeit": "10:00"},
        )
        booked = None
        for sl in (slot_res.get("slots") or [])[:5]:
            payload = {
                "name": KUNDE_NAME, "anliegen": "Storno-Chat-Test",
                "datum": sl["datum"], "uhrzeit": sl["uhrzeit"],
                "kunde_email": KUNDE_EMAIL,
                "idempotency_key": f"storno-chat-test-{datetime.now().isoformat()}",
            }
            if sl.get("dauer_minuten"):
                payload["dauer_minuten"] = sl["dauer_minuten"]
            res = await kalender.on_webhook("book_appointment", payload)
            if res.get("erfolg"):
                booked = (sl, res.get("event_id"))
                break
        if not booked:
            print("FEHLER: Konnte keinen Termin buchen")
            return 4
        sl, _eid = booked
        print(f"Neu gebucht: {sl['datum']} {sl['uhrzeit']}")
        find_res = await kalender.on_webhook("find_events", {"kunde_name": KUNDE_NAME})
        termine = find_res.get("termine", [])

    print(f"\nSven-Termine im Kalender ({len(termine)}):")
    for t in termine:
        conv, created = await _ensure_conversation(tenant.id, t["event_id"])
        flag = "NEU verknuepft" if created else "schon verknuepft"
        print(f"  - {t['start_dt']} | {t.get('summary')} | Mail-Link: {flag}")

    print(f"\nBereit. Jetzt im Telegram-Chat des Betriebs '{tenant.slug}':")
    print("  /storno  ->  'Sven Jantos'  ->  Nummer waehlen  ->  ja")
    print(f"  Danach bekommt {KUNDE_EMAIL} die Storno-Bestaetigung.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
