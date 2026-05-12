"""Test des Onboarding-Tutorials:
1. /onboarding startet → Welcome-Buttons
2. Tap "Los geht's" → Schritt 1 (Firmenname)
3. Schritte 1-7 mit Text-Eingaben
4. Schritt 7 (Branche) per Button
5. Schritt 8 (Lexware): "Spaeter" Button
6. Schritt 9 (Kalender): "Spaeter" Button
7. Schritt 10 (Knowledge): /skip
8. Done — Tenant.onboarding_completed_at gesetzt + Impressum-Eintrag in DB
9. Andere Befehle waehrend des Onboardings werden geblockt
10. /hilfe zeigt Step-spezifischen Hilfe-Text
"""
from __future__ import annotations

import asyncio
import time
import uuid as _uuid
from decimal import Decimal

from plugins.telegram_notify import handler as h
from plugins.telegram_notify.handler import (
    TelegramNotifier, process_telegram_update, _clear_state,
)
from sqlalchemy import select
from core.database import AsyncSessionLocal
from core.models import Tenant, TenantKnowledge

SVEN = 8518191832
captured: list[tuple[int, str]] = []

async def fake_send_raw(bot_token, chat_id, text):
    captured.append((int(chat_id), str(text)))
    return True
TelegramNotifier._send_raw = staticmethod(fake_send_raw)

import httpx as _httpx
async def fake_post(self, url, *args, **kwargs):
    if "sendMessage" in url and "json" in kwargs:
        p = kwargs["json"]
        captured.append((int(p.get("chat_id", 0)), str(p.get("text", ""))))
    class R:
        status_code = 200
        text = "{}"
        def json(self): return {}
    return R()
_httpx.AsyncClient.post = fake_post


def upd_text(text):
    return {"update_id": int(time.time()*1000),
            "message": {"message_id": 1,
                        "from": {"id": SVEN, "is_bot": False},
                        "chat": {"id": SVEN, "type": "private"},
                        "date": int(time.time()), "text": text}}


def upd_callback(data):
    return {"update_id": int(time.time()*1000),
            "callback_query": {"id": "cq-t", "from": {"id": SVEN, "is_bot": False},
                               "data": data,
                               "message": {"message_id": 1,
                                           "chat": {"id": SVEN, "type": "private"}}}}


async def main():
    print("=" * 78)
    print("Onboarding-Tutorial Test")
    print("=" * 78)
    await _clear_state(SVEN)

    # Fixtures: Tenant zurueck auf "nicht onboarded" setzen.
    # Snapshot ALLER Felder die das Onboarding ueberschreibt, damit
    # der Test sie am Ende wiederherstellen kann (sonst pollution).
    from plugins.telegram_notify.handler import _get_tenant_by_chat
    tenant = await _get_tenant_by_chat(SVEN)
    snapshot_fields = (
        "onboarding_completed_at", "onboarding_step",
        "company_name", "contact_name", "contact_email", "contact_phone",
        "heimat_strasse", "heimat_plz", "heimat_ort", "branche",
    )
    async with AsyncSessionLocal() as s:
        t = (await s.execute(select(Tenant).where(Tenant.id == tenant.id))).scalar_one()
        original_values = {f: getattr(t, f) for f in snapshot_fields}
        t.onboarding_completed_at = None
        t.onboarding_step = 0
        await s.commit()
    print(f"Tenant '{tenant.slug}' fuer Test auf step=0 + completed=None gesetzt")
    print(f"  Snapshot: {len(snapshot_fields)} Felder zur Wiederherstellung gespeichert")

    # T1: /onboarding → Welcome
    print("\n--- T1: /onboarding ---")
    captured.clear()
    await process_telegram_update(upd_text("/onboarding"))
    print(captured[-1][1][:500] if captured else "(silent)")

    # T2: Tap "Los geht's"
    print("\n--- T2: callback ob:start → Schritt 1 (Firmenname) ---")
    captured.clear()
    await process_telegram_update(upd_callback("ob:start"))
    print(captured[-1][1][:400] if captured else "(silent)")

    # T3: Firmenname eingeben
    print("\n--- T3: Firmenname 'Schreinerei Test GbR' ---")
    captured.clear()
    await process_telegram_update(upd_text("Schreinerei Test GbR"))
    print(captured[-1][1][:300] if captured else "(silent)")

    # T4: Inhaber-Name eingeben (voller Name Pflicht — erst nur "Test", dann voll)
    print("\n--- T4a: 'Test' (zu kurz) ---")
    captured.clear()
    await process_telegram_update(upd_text("Test"))
    print(captured[-1][1][:200] if captured else "(silent)")

    print("\n--- T4b: 'Anna Test' (voller Name) ---")
    captured.clear()
    await process_telegram_update(upd_text("Anna Test"))
    print(captured[-1][1][:300] if captured else "(silent)")

    # T5: Strasse
    print("\n--- T5: Strasse 'Hauptstr 5' ---")
    captured.clear()
    await process_telegram_update(upd_text("Hauptstr 5"))
    print(captured[-1][1][:200] if captured else "(silent)")

    # T6: PLZ Ort
    print("\n--- T6: PLZ Ort '54290 Trier' ---")
    captured.clear()
    await process_telegram_update(upd_text("54290 Trier"))
    print(captured[-1][1][:200] if captured else "(silent)")

    # T7: Telefon /skip
    print("\n--- T7: /skip Telefon ---")
    captured.clear()
    await process_telegram_update(upd_text("/skip"))
    print(captured[-1][1][:200] if captured else "(silent)")

    # T8: Email
    print("\n--- T8: Email 'test@example.com' ---")
    captured.clear()
    await process_telegram_update(upd_text("test@example.com"))
    print(captured[-1][1][:200] if captured else "(silent)")

    # T9: Branche tischler
    print("\n--- T9: ob:branche:tischler ---")
    captured.clear()
    await process_telegram_update(upd_callback("ob:branche:tischler"))
    print(captured[-1][1][:300] if captured else "(silent)")

    # T9.5: Block-Test — anderer Befehl waehrend Onboarding
    print("\n--- T9.5: /aufnahme waehrend Onboarding (sollte geblockt sein) ---")
    captured.clear()
    await process_telegram_update(upd_text("/aufnahme"))
    print(captured[-1][1][:300] if captured else "(silent)")

    # T10: /hilfe (zeigt Erklaerung zu Lexware)
    print("\n--- T10: /hilfe ---")
    captured.clear()
    await process_telegram_update(upd_text("/hilfe"))
    print(captured[-1][1][:400] if captured else "(silent)")

    # T11: Lexware: spaeter
    print("\n--- T11: ob:lexware:skip ---")
    captured.clear()
    await process_telegram_update(upd_callback("ob:lexware:skip"))
    print(captured[-1][1][:200] if captured else "(silent)")

    # T12: Kalender: spaeter
    print("\n--- T12: ob:kalender:skip ---")
    captured.clear()
    await process_telegram_update(upd_callback("ob:kalender:skip"))
    print(captured[-1][1][:200] if captured else "(silent)")

    # T13: Knowledge /skip
    print("\n--- T13: /skip Knowledge ---")
    captured.clear()
    await process_telegram_update(upd_text("/skip"))
    print(captured[-1][1][:500] if captured else "(silent)")

    # Verify endstatus
    async with AsyncSessionLocal() as s:
        t = (await s.execute(select(Tenant).where(Tenant.id == tenant.id))).scalar_one()
        print(f"\n=== Final Tenant-Status ===")
        print(f"  onboarding_step:         {t.onboarding_step}")
        print(f"  onboarding_completed_at: {t.onboarding_completed_at}")
        print(f"  company_name:            {t.company_name!r}")
        print(f"  contact_name:            {t.contact_name!r}")
        print(f"  heimat_strasse:          {t.heimat_strasse!r}")
        print(f"  heimat_plz/ort:          {t.heimat_plz!r} {t.heimat_ort!r}")
        print(f"  contact_email:           {t.contact_email!r}")
        print(f"  branche:                 {t.branche!r}")

        # Impressum-Knowledge-Eintrag
        from core.models.tenant_knowledge import KATEGORIE_BESONDERHEITEN
        impressum = (await s.execute(
            select(TenantKnowledge).where(
                TenantKnowledge.tenant_id == tenant.id,
                TenantKnowledge.kategorie == KATEGORIE_BESONDERHEITEN,
                TenantKnowledge.text.like("Firma:%"),
            )
        )).scalars().first()
        if impressum:
            print(f"\n=== Impressum-Knowledge-Eintrag ===")
            print(impressum.text)
        else:
            print("\n  (kein Impressum-Eintrag — ggf. nicht angelegt)")

        # Alle Snapshot-Felder zuruecksetzen — verhindert Test-Pollution
        for fname, fval in original_values.items():
            setattr(t, fname, fval)
        if impressum:
            await s.delete(impressum)
        await s.commit()
    print("\n--- Original Tenant-State (alle Felder) wiederhergestellt ---")


asyncio.run(main())
