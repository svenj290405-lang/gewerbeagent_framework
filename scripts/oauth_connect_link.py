"""Signierten Verbindungs-Link fuer Google/Outlook erzeugen.

Der oeffentliche Einstieg `GET /oauth/start` braucht seit dem Audit vom
2026-08-29 eine Signatur. Grund: der Hijack-Schutz greift erst ab der
ZWEITEN Verbindung, die ERSTE war fuer jeden offen, der einen Tenant-Slug
erraet — und Slugs sind kurz und sprechend. Wer das tat, haengte sein
eigenes Konto an einen fremden Betrieb; dessen Kundenmails waeren danach
ueber sein Postfach rausgegangen und dessen Kundenfotos in sein Drive.

Der Link bleibt teilbar (Mail, WhatsApp) und gilt eine Woche.
Der Weg aus der App heraus (Einstellungen -> Verbindungen) braucht ihn
nicht — der ist authentifiziert.

Nutzung (im Prod-Container):
  # Betriebe + bestehende Anbindungen zeigen:
  docker exec gewerbeagent_framework /app/.venv/bin/python \
      /app/scripts/oauth_connect_link.py --list

  # Link fuer den Inhaber eines Betriebs:
  docker exec gewerbeagent_framework /app/.venv/bin/python \
      /app/scripts/oauth_connect_link.py <tenant_slug> google

  # Link fuer einen einzelnen Mitarbeiter:
  docker exec gewerbeagent_framework /app/.venv/bin/python \
      /app/scripts/oauth_connect_link.py <tenant_slug> microsoft <employee_slug>
"""
from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from core.database.connection import get_session
from core.models.employee import Employee
from core.models.oauth_token import OAuthToken
from core.models.tenant import Tenant
from core.security.oauth_flow import CONNECT_LINK_TTL_SECONDS, make_connect_link


async def _list() -> None:
    async with get_session() as s:
        tenants = (await s.execute(select(Tenant).order_by(Tenant.slug))).scalars().all()
        for t in tenants:
            print(f"\n{t.slug}  ({t.company_name or '—'})")
            emps = (await s.execute(
                select(Employee)
                .where(Employee.tenant_id == t.id)
                .order_by(Employee.is_default.desc(), Employee.slug)
            )).scalars().all()
            for e in emps:
                toks = (await s.execute(
                    select(OAuthToken).where(OAuthToken.employee_id == e.id)
                )).scalars().all()
                verbunden = ", ".join(
                    f"{tok.provider}={tok.account_email or '(unfertig)'}"
                    for tok in toks
                ) or "keine Anbindung"
                rolle = "Inhaber" if e.is_default else (e.role or "—")
                print(f"    {e.slug:<20} {rolle:<10} {verbunden}")


async def _main() -> None:
    args = [a for a in sys.argv[1:] if a]
    if not args or args[0] in ("--list", "-l"):
        await _list()
        return

    tenant_slug = args[0]
    provider = args[1] if len(args) > 1 else "google"
    employee_slug = args[2] if len(args) > 2 else None

    if provider not in ("google", "microsoft"):
        print(f"Unbekannter Provider: {provider!r} (google|microsoft)")
        sys.exit(2)

    # Existenz pruefen, damit ein Tippfehler nicht als toter Link rausgeht.
    async with get_session() as s:
        t = (await s.execute(
            select(Tenant).where(Tenant.slug == tenant_slug)
        )).scalar_one_or_none()
        if t is None:
            print(f"Betrieb {tenant_slug!r} gibt es nicht. --list zeigt alle.")
            sys.exit(1)
        if employee_slug:
            e = (await s.execute(select(Employee).where(
                Employee.tenant_id == t.id, Employee.slug == employee_slug,
            ))).scalar_one_or_none()
            if e is None:
                print(f"Mitarbeiter {employee_slug!r} gibt es in {tenant_slug!r} nicht.")
                sys.exit(1)

    link = make_connect_link(tenant_slug, provider, employee_slug)
    tage = CONNECT_LINK_TTL_SECONDS // 86400
    print(f"\n{link}\n")
    print(f"Gueltig {tage} Tage. Wer ihn oeffnet, verbindet SEIN {provider}-Konto")
    print(f"mit {tenant_slug!r} — also nur an den Betrieb selbst schicken.")


if __name__ == "__main__":
    asyncio.run(_main())
