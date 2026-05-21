"""Sendet zwei Test-Mails durch die neue Dialog-Pipeline:
1. Wissensfrage ("wann seid ihr offen?") — Q sollte ASK_MORE waehlen
   (Antwort OHNE Formular-Button)
2. Echte Anfrage ("ich brauche eine neue Kueche") — Q sollte
   SEND_FORMULAR waehlen (Antwort MIT Button)

Ruft handle_kunde_mail_dialog direkt (kein Inbox-Poll), umgeht die
Pipeline-Wire-up — verifiziert nur Gemini-Entscheidung + Template-
Rendering. Pipeline-Integration siehe e2e via echte Inbound-Mail.

Aufruf:
    uv run python scripts/test_dialog_send.py [empfaenger]
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import datetime

from sqlalchemy import select

from core.ai.gemini import handle_kunde_mail_dialog
from core.database import AsyncSessionLocal
from core.integrations.anfrage_forms import (
    build_anfrage_url,
    create_anfrage_token,
)
from core.integrations.mail_template import (
    build_kunde_reply_html,
    extract_first_name,
)
from core.integrations.microsoft import send_mail_as_user
from core.models import ANFRAGE_TYP_ALLGEMEIN, OAuthToken, Tenant


CASES = [
    {
        "label": "Wissensfrage (erwartet ASK_MORE, kein Button)",
        "subject": "Frage zu Oeffnungszeiten",
        "body": (
            "Hallo, ich wollte nur kurz fragen: wann seid ihr von Mo bis "
            "Fr offen? Brauche aktuell nichts konkretes, will nur "
            "vorbereiten."
        ),
    },
    {
        "label": "Echte Anfrage (erwartet SEND_FORMULAR, mit Button)",
        "subject": "Anfrage neue Werkbank",
        "body": (
            "Hallo, ich brauche fuer meine Werkstatt eine massive Werkbank, "
            "ca. 2,40 m breit. Ahorn oder Buche. Koennt ihr mir ein "
            "Angebot machen? Lieferung nach Trier waere super."
        ),
    },
]


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

    for idx, case in enumerate(CASES, start=1):
        print(f"=== Case {idx}: {case['label']} ===")
        dialog = await handle_kunde_mail_dialog(
            subject=case["subject"],
            sender_name="Sven",
            sender_email=to,
            latest_message=case["body"],
            tenant_company=tenant.company_name,
            tenant_owner_first_name=owner_first or None,
            tenant_branche=getattr(tenant, "branche", None) or "Handwerk",
            wissensbasis=(
                "- [Oeffnungszeiten] Mo-Fr 7-17 Uhr, Sa 9-13 Uhr\n"
                "- [Lieferung] Wir liefern im Umkreis 50km um Trier, "
                "Anfahrt nach Vereinbarung."
            ),
        )
        print(f"  next_action: {dialog['next_action']}")
        print(f"  anrede_form: {dialog['anrede_form']}")
        print(f"  reason:      {dialog.get('reason')!r}")
        print(f"  reply_text ({len(dialog['reply_text'])} chars):")
        for line in dialog["reply_text"].split("\n"):
            print(f"    {line}")

        # Token nur bei SEND_FORMULAR
        form_url = ""
        with_button = dialog["next_action"] == "SEND_FORMULAR"
        if with_button:
            tok = await create_anfrage_token(
                tenant_id=tenant.id,
                kunde_email=to,
                kunde_name="Sven (Test)",
                anfrage_typ=ANFRAGE_TYP_ALLGEMEIN,
                original_subject=case["subject"],
                original_message_id=None,
                valid_days=14,
            )
            form_url = build_anfrage_url(tok.token)

        body_html = build_kunde_reply_html(
            kunde_anrede_name="Sven",
            kunde_email=to,
            reply_text=dialog["reply_text"],
            form_url=form_url,
            company_name=tenant.company_name,
            contact_name=getattr(tenant, "contact_name", "") or "",
            contact_email=getattr(tenant, "contact_email", "") or "",
            contact_phone=getattr(tenant, "contact_phone", "") or "",
            with_formular_button=with_button,
        )

        stamp = datetime.now().strftime("%H:%M:%S")
        subject_out = f"[Phase1-Dialog {idx} {stamp}] {case['subject']}"
        ok = await send_mail_as_user(
            tenant_id=tenant.id,
            to_email=to,
            subject=subject_out,
            body_html=body_html,
            employee_id=token.employee_id,
        )
        print(f"  Sendmail: success={ok}  subject={subject_out!r}")
        print()
        # Kurze Pause damit beide Mails nicht im selben Bundle landen
        await asyncio.sleep(1.0)

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
