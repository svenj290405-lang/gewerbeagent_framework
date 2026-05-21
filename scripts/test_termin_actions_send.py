"""Sendet drei Test-Mails durch den Phase-2 Termin-Action-Pfad:

1. PROPOSE_SLOTS — "wann habt ihr Donnerstag vormittag Zeit?"
   -> Antwort mit Slot-Liste, KEIN Formular-Button
2. BOOK_SLOT — Folge-Turn auf 1.: "der erste passt"
   -> Antwort mit "Termin bestätigt"-Box, Eintrag im Kalender
3. CANCEL_TERMIN — "ich muss leider absagen"
   -> Antwort mit "Termin storniert"-Box, Kalender-Loeschung

Ruft handle_kunde_mail_dialog + die Pipeline-Hilfsfunktionen direkt
(kein Inbox-Poll), umgeht aber den vollen process_relevant_kunde_mail-
Pfad. Verifiziert Gemini-Entscheidung + Tool-Call + Template-Rendering.

Aufruf:
    uv run python scripts/test_termin_actions_send.py [empfaenger]
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta

from sqlalchemy import select

from core.ai.gemini import handle_kunde_mail_dialog
from core.database import AsyncSessionLocal
from core.integrations.mail_template import (
    build_kunde_reply_html,
    extract_first_name,
)
from core.integrations.microsoft import send_tracked_mail
from core.models import OAuthToken, Tenant
from core.plugin_system import get_plugin_for_tenant


async def _render_and_send(*, tenant, owner_first, to, subject, dialog,
                           slot_proposals=None, booked_termin=None,
                           storno_summary=None):
    body_html = build_kunde_reply_html(
        kunde_anrede_name="Sven",
        kunde_email=to,
        reply_text=dialog["reply_text"],
        form_url="",
        company_name=tenant.company_name,
        contact_name=getattr(tenant, "contact_name", "") or "",
        contact_email=getattr(tenant, "contact_email", "") or "",
        contact_phone=getattr(tenant, "contact_phone", "") or "",
        with_formular_button=False,
        slot_proposals=slot_proposals,
        booked_termin=booked_termin,
        storno_summary=storno_summary,
    )
    res = await send_tracked_mail(
        tenant_id=tenant.id,
        to_email=to,
        subject=subject,
        body_html=body_html,
    )
    print(f"  send_tracked_mail success={res.get('success')} "
          f"imsg={res.get('internet_message_id')}")
    return res


async def main() -> int:
    to = sys.argv[1] if len(sys.argv) > 1 else "svenj05@gmx.de"

    async with AsyncSessionLocal() as s:
        row = (await s.execute(
            select(Tenant, OAuthToken)
            .join(OAuthToken, OAuthToken.tenant_id == Tenant.id)
            .where(OAuthToken.provider == "microsoft")
            .order_by(OAuthToken.employee_id.is_(None).desc())
        )).first()
    if not row:
        print("Kein Microsoft-Token")
        return 2
    tenant, token = row
    owner_first = extract_first_name(getattr(tenant, "contact_name", "") or "")

    print(f"Tenant: {tenant.slug} ({tenant.company_name})")
    print(f"Postfach: {token.account_email}")
    print(f"Empfaenger: {to}")
    print()

    kalender = await get_plugin_for_tenant(tenant.slug, "kalender")
    if kalender is None:
        print(f"Kein kalender-Plugin fuer Tenant {tenant.slug}")
        return 3

    # =====================================================================
    # Case 1: PROPOSE_SLOTS
    # =====================================================================
    print("=== Case 1: PROPOSE_SLOTS — Kunde fragt nach Termin ===")
    case1_subject = "[Phase2-Termin 1] Wann habt ihr Zeit?"
    case1_body = (
        "Hallo, koennt ihr mir naechste Woche Donnerstag vormittag "
        "einen Termin geben? Es geht um eine kleine Reparatur an "
        "der Werkbank."
    )
    dialog1 = await handle_kunde_mail_dialog(
        subject=case1_subject,
        sender_name="Sven",
        sender_email=to,
        latest_message=case1_body,
        tenant_company=tenant.company_name,
        tenant_owner_first_name=owner_first or None,
        tenant_branche=getattr(tenant, "branche", None) or "Handwerk",
        wissensbasis=(
            "- [Oeffnungszeiten] Mo-Fr 7-17 Uhr\n"
            "- [Werkstatt] Adresse: Im Gewerbepark 12, Trier"
        ),
    )
    print(f"  next_action={dialog1['next_action']} "
          f"wunsch_datum={dialog1.get('wunsch_datum')} "
          f"wunsch_uhrzeit={dialog1.get('wunsch_uhrzeit')}")

    # Anker fuer find_free_slots
    wd = dialog1.get("wunsch_datum") or (
        datetime.now() + timedelta(days=1)
    ).strftime("%d.%m.%Y")
    wt = dialog1.get("wunsch_uhrzeit") or "09:00"
    slot_res = await kalender.on_webhook(
        "find_free_slots", {"datum": wd, "uhrzeit": wt},
    )
    raw_slots = list(slot_res.get("slots") or [])[:4]
    wochentage = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
    slots = []
    for sl in raw_slots:
        wtag = ""
        try:
            d = datetime.strptime(sl.get("datum", ""), "%d.%m.%Y").date()
            wtag = wochentage[d.weekday()]
        except Exception:
            pass
        slots.append({
            "datum": sl.get("datum", ""),
            "uhrzeit": sl.get("uhrzeit", ""),
            "wochentag": wtag,
            "dauer_minuten": sl.get("dauer_minuten"),
            "employee_id": str(sl.get("employee_id")) if sl.get("employee_id") else None,
        })
    print(f"  find_free_slots -> {len(slots)} Slot(s)")

    if not slots:
        print("  Keine freien Slots — uebersprungen.")
    else:
        await _render_and_send(
            tenant=tenant, owner_first=owner_first, to=to,
            subject=f"Re: {case1_subject}",
            dialog=dialog1,
            slot_proposals=slots,
        )

    print()
    # =====================================================================
    # Case 2: BOOK_SLOT
    # =====================================================================
    print("=== Case 2: BOOK_SLOT — Kunde bestaetigt ersten Slot ===")
    if not slots:
        print("  keine Slots aus Case 1 — uebersprungen.")
    else:
        chosen = slots[0]
        case2_body = "Hallo, der erste Termin passt mir, bitte buchen."
        dialog2 = await handle_kunde_mail_dialog(
            subject=f"Re: {case1_subject}",
            sender_name="Sven",
            sender_email=to,
            latest_message=case2_body,
            tenant_company=tenant.company_name,
            tenant_owner_first_name=owner_first or None,
            tenant_branche=getattr(tenant, "branche", None) or "Handwerk",
            wissensbasis="-",
            previous_anrede_form=dialog1.get("anrede_form"),
            previous_proposed_slots=slots,
        )
        print(f"  next_action={dialog2['next_action']} "
              f"chosen_slot_index={dialog2.get('chosen_slot_index')}")
        # Buchen
        book_payload = {
            "name": f"Sven (Phase2-Test {datetime.now().strftime('%H:%M:%S')})",
            "anliegen": "Phase2-Mail-Dialog-Buchungstest",
            "datum": chosen["datum"],
            "uhrzeit": chosen["uhrzeit"],
            "dauer_minuten": chosen.get("dauer_minuten"),
            "kunde_email": to,
            "idempotency_key": f"phase2test-{datetime.now().isoformat()}",
        }
        book_res = await kalender.on_webhook("book_appointment", book_payload)
        print(f"  book_appointment erfolg={book_res.get('erfolg')} "
              f"event_id={book_res.get('event_id')}")
        booked_termin = {
            "datum": chosen["datum"], "uhrzeit": chosen["uhrzeit"],
            "anliegen": book_payload["anliegen"],
        } if book_res.get("erfolg") else None
        if booked_termin:
            await _render_and_send(
                tenant=tenant, owner_first=owner_first, to=to,
                subject=f"Re: {case1_subject}",
                dialog=dialog2,
                booked_termin=booked_termin,
            )

    print()
    # =====================================================================
    # Case 3: CANCEL_TERMIN
    # =====================================================================
    print("=== Case 3: CANCEL_TERMIN — Kunde sagt ab ===")
    case3_subject = "[Phase2-Termin 3] Muss leider absagen"
    case3_body = "Hallo, ich muss meinen Termin doch leider absagen."
    dialog3 = await handle_kunde_mail_dialog(
        subject=case3_subject,
        sender_name="Sven",
        sender_email=to,
        latest_message=case3_body,
        tenant_company=tenant.company_name,
        tenant_owner_first_name=owner_first or None,
        tenant_branche=getattr(tenant, "branche", None) or "Handwerk",
        wissensbasis="-",
    )
    print(f"  next_action={dialog3['next_action']}")
    from core.integrations.mail_pipeline import cancel_kunde_termine
    cancelled = await cancel_kunde_termine(tenant, to, None)
    print(f"  cancel_kunde_termine -> {len(cancelled)} Termine geloescht")
    await _render_and_send(
        tenant=tenant, owner_first=owner_first, to=to,
        subject=f"Re: {case3_subject}",
        dialog=dialog3,
        storno_summary={"cancelled_count": len(cancelled)},
    )

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
