"""Test-Mail an einen Ziel-Empfaenger ueber die echte Microsoft-Pipeline.

Sucht den ersten Tenant mit verbundenem Microsoft-Postfach (ohne
employee_id, also den Default-Tenant-Mailbox) und schickt eine
Test-Mail via send_tracked_mail — der gleiche Two-Step-Pfad
(Draft-Create + Send), den auch die produktiven Q-Replies nutzen.

Aufruf:
    uv run python scripts/send_test_mail.py [empfaenger@host]

Default-Empfaenger: svenj05@gmx.de
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime

from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.integrations.microsoft import send_tracked_mail
from core.models import OAuthToken, Tenant


async def pick_tenant_with_microsoft() -> tuple[Tenant, OAuthToken] | None:
    """Erstes (Tenant, OAuthToken)-Paar mit Microsoft-Verbindung.

    Bevorzugt Token ohne employee_id (Tenant-Default-Postfach); fallt
    zurueck auf irgendeinen Mitarbeiter-Token wenn keiner ohne
    employee_id existiert.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Tenant, OAuthToken)
            .join(OAuthToken, OAuthToken.tenant_id == Tenant.id)
            .where(OAuthToken.provider == "microsoft")
            .order_by(OAuthToken.employee_id.is_(None).desc(), Tenant.created_at)
        )
        row = result.first()
        return (row[0], row[1]) if row else None


async def main() -> int:
    to = sys.argv[1] if len(sys.argv) > 1 else "svenj05@gmx.de"

    picked = await pick_tenant_with_microsoft()
    if not picked:
        print("FEHLER: Kein Tenant mit verbundenem Microsoft-Postfach gefunden.")
        return 2
    tenant, token = picked

    emp_str = f" (employee={token.employee_id})" if token.employee_id else " (tenant-default)"
    print(f"Tenant:   {tenant.slug} ({tenant.company_name})")
    print(f"Postfach: {token.account_email}{emp_str}")
    print(f"Empfaenger: {to}")

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    subject = f"[Pipeline-Test] {stamp}"
    body_html = f"""<!DOCTYPE html>
<html><body style="font-family:sans-serif;color:#222">
<h2>Pipeline-Test</h2>
<p>Hallo Sven,</p>
<p>diese Mail wurde von <b>{tenant.company_name}</b> ({tenant.slug})
ueber die produktive Microsoft-Graph-Pipeline gesendet
({stamp}).</p>
<p><b>Was du jetzt testen kannst:</b></p>
<ol>
  <li>Direkt aus diesem Postfach <b>antworten</b> (Reply) — der
      Inbox-Poller soll den Reply als Folge-Mail derselben
      Konversation erkennen (In-Reply-To/conversationId-Threading).</li>
  <li>Eine <b>neue</b> Mail mit eigenem Subject (z.B. "Termin am
      Freitag?") an dasselbe Postfach schicken — soll als
      RELEVANT_KUNDE klassifiziert werden und eine Anfrage-Antwort
      mit Formular-Link ausloesen.</li>
  <li>Eine Mail mit "Termin stornieren" oder "Storno fuer Termin
      am ..." schicken — soll den Storno-Handler triggern.</li>
</ol>
<p>Der naechste Poll-Lauf passiert spaetestens alle 2 Minuten
(microsoft_cron).</p>
<p>Gruss<br>Gewerbeagent-Pipeline-Smoke-Test</p>
</body></html>
"""
    result = await send_tracked_mail(
        tenant_id=tenant.id,
        to_email=to,
        subject=subject,
        body_html=body_html,
        employee_id=token.employee_id,
    )

    print()
    print(f"success:             {result.get('success')}")
    print(f"message_id:          {result.get('message_id')}")
    print(f"internet_message_id: {result.get('internet_message_id')}")
    print(f"conversation_id:     {result.get('conversation_id')}")
    if result.get("error"):
        print(f"error:               {result['error']}")

    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
