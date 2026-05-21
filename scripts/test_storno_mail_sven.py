"""End-to-End-Test der Storno-Bestaetigungs-Mail.

Ablauf (genau der /storno-Wizard-Pfad):
  1. Termin fuer Sven Jantos (svenj05@gmx.de) buchen.
  2. EmailConversation an die event_id haengen (sonst kann die Storno-Mail
     die Kundenadresse nicht aufloesen — direkt gebuchte Termine haben keine).
  3. Termin stornieren (cancel_appointment).
  4. Storno-Bestaetigungs-Mail an svenj05@gmx.de schicken
     (send_storno_confirmation_for_event — derselbe Helfer wie im Wizard).

Schickt eine ECHTE Mail vom Test-Postfach an svenj05@gmx.de.

Aufruf (im Container):
    uv run python -m scripts.test_storno_mail_sven
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
        print("FEHLER: Kein Microsoft-Test-Betrieb gefunden")
        return 2
    tenant, token = row
    print(f"Betrieb:  {tenant.slug} ({tenant.company_name})")
    print(f"Postfach: {token.account_email}  ->  Kunde: {KUNDE_EMAIL}")

    kalender = await get_plugin_for_tenant(tenant.slug, "kalender")
    if kalender is None:
        print("FEHLER: Kein kalender-Plugin")
        return 3

    # 1) Termin buchen
    anker = (datetime.now() + timedelta(days=2)).strftime("%d.%m.%Y")
    slot_res = await kalender.on_webhook(
        "find_free_slots", {"datum": anker, "uhrzeit": "10:00"},
    )
    slots = list(slot_res.get("slots") or [])
    event_id = None
    chosen = None
    for sl in slots[:5]:
        payload = {
            "name": KUNDE_NAME,
            "anliegen": "Storno-Mail-Test",
            "datum": sl["datum"],
            "uhrzeit": sl["uhrzeit"],
            "kunde_email": KUNDE_EMAIL,
            "idempotency_key": f"storno-mail-test-{datetime.now().isoformat()}",
        }
        if sl.get("dauer_minuten"):
            payload["dauer_minuten"] = sl["dauer_minuten"]
        res = await kalender.on_webhook("book_appointment", payload)
        if res.get("erfolg"):
            event_id = res.get("event_id")
            chosen = sl
            break
    if not event_id:
        print("FEHLER: Buchung fehlgeschlagen (keine freien Slots buchbar)")
        return 4
    print(f"1) Gebucht: {chosen['datum']} {chosen['uhrzeit']} -> event_id={event_id}")

    # 2) EmailConversation verknuepfen
    from core.integrations.mail_pipeline import create_conversation
    conv = await create_conversation(
        tenant.id, KUNDE_EMAIL, KUNDE_NAME, "Ihre Terminbuchung",
        gcal_event_id=event_id,
    )
    print(f"2) EmailConversation verknuepft: id={conv.id} kunde={conv.kunde_email}")

    # 3) Storno (wie der /storno-Wizard)
    cancel_res = await kalender.on_webhook(
        "cancel_appointment", {"event_id": event_id},
    )
    print(f"3) Storno: cancel_appointment erfolg={cancel_res.get('erfolg')}")

    # 4) Storno-Bestaetigungs-Mail an den Kunden
    from core.integrations.mail_pipeline import send_storno_confirmation_for_event
    sent = await send_storno_confirmation_for_event(
        tenant_id=tenant.id,
        company_name=tenant.company_name or "",
        event_id=event_id,
        cancelled_count=1,
    )
    print(
        f"4) Storno-Bestaetigungs-Mail an {KUNDE_EMAIL}: "
        f"{'GESENDET ✓' if sent else 'NICHT gesendet ✗'}"
    )
    return 0 if sent else 6


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
