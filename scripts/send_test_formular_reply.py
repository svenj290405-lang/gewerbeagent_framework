"""Erzeugt eine echte Q-Reply (Formular-Mail) wie die Pipeline sie baut
und schickt sie an einen Test-Empfaenger.

Pfad: simuliert eine eingegangene Kunden-Anfrage, ruft
`generate_anfrage_reply` (Gemini) + `build_kunde_reply_html` auf und
sendet das Ergebnis via `send_mail_as_user`. So sehen wir 1:1 das
Rendering das ein echter Kunde nach dem Fix bekommt.

Aufruf:
    uv run python scripts/send_test_formular_reply.py [empfaenger]
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import datetime

from sqlalchemy import select

from core.ai.gemini import generate_anfrage_reply
from core.database import AsyncSessionLocal
from core.integrations.mail_template import (
    build_kunde_reply_html,
    extract_first_name,
)
from core.integrations.microsoft import send_mail_as_user
from core.models import OAuthToken, Tenant


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

    # Test-Anfrage simulieren — bewusst mit "Du", damit Gemini Du-Form
    # konsistent zieht. Inhaltlich plausibel fuer eine Schreinerei.
    simulated_subject = "Anfrage neue Werkbank"
    simulated_body = (
        "Hallo,\n\n"
        "ich brauche fuer meine Werkstatt eine massive Werkbank, "
        "ca. 2,40 m breit. Ahorn oder Buche. Koennt ihr mir ein "
        "Angebot machen? Lieferung nach Trier waere super.\n\n"
        "Gruss\nSven"
    )
    sender_email = to
    sender_name = "Sven"

    # Form-URL bauen (Format wie in der echten Pipeline)
    fake_token = uuid.uuid4().hex[:22]
    form_url = f"https://gewerbeagent.de/anfrage/{fake_token}"

    owner_first = extract_first_name(getattr(tenant, "contact_name", "") or "")
    print(f"Tenant:   {tenant.slug} ({tenant.company_name})")
    print(f"Postfach: {token.account_email} (employee={token.employee_id})")
    print(f"Owner:    {owner_first or '(kein contact_name)'}")
    print(f"Empfaenger: {to}")
    print(f"Form-URL: {form_url}")
    print()
    print("Generiere Reply via Gemini ...")

    reply_text = await generate_anfrage_reply(
        subject=simulated_subject,
        sender_name=sender_name,
        sender_email=sender_email,
        body=simulated_body,
        form_url=form_url,
        tenant_company=tenant.company_name,
        tenant_owner_first_name=owner_first or None,
    )

    print(f"--- Reply-Text (Plain, {len(reply_text)} Zeichen) ---")
    print(reply_text)
    print("--- /Reply-Text ---")
    print()

    body_html = build_kunde_reply_html(
        kunde_anrede_name=sender_name,
        kunde_email=sender_email,
        reply_text=reply_text,
        form_url=form_url,
        company_name=tenant.company_name,
        contact_name=getattr(tenant, "contact_name", "") or "",
        contact_email=getattr(tenant, "contact_email", "") or "",
        contact_phone=getattr(tenant, "contact_phone", "") or "",
        contact_website=getattr(tenant, "contact_website", "") or "",
    )

    stamp = datetime.now().strftime("%H:%M:%S")
    subject = f"Re: {simulated_subject} [Pipeline-Fix-Test {stamp}]"
    ok = await send_mail_as_user(
        tenant_id=tenant.id,
        to_email=to,
        subject=subject,
        body_html=body_html,
        employee_id=token.employee_id,
    )

    print(f"Sendmail: success={ok}  subject={subject!r}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
