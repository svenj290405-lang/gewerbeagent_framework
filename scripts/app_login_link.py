"""Direkten PWA-Login-Link erzeugen (ohne Mail).

Erstellt einen einmaligen AppLoginToken fuer den Inhaber-Account
(is_default) eines Betriebs und gibt die fertige Login-URL aus. Einfach
auf dem Handy antippen -> eingeloggt. Umgeht die Magic-Link-Mail komplett
(nuetzlich zum Testen oder wenn der Mailversand klemmt).

Nutzung (im Prod-Container):
  # Betriebe auflisten:
  docker exec gewerbeagent_framework /app/.venv/bin/python \
      /app/scripts/app_login_link.py --list

  # Login-Link fuer einen Betrieb erzeugen:
  docker exec gewerbeagent_framework /app/.venv/bin/python \
      /app/scripts/app_login_link.py <tenant_slug>
"""
from __future__ import annotations

import asyncio
import datetime as dt
import secrets
import sys

from sqlalchemy import select

from config.settings import settings
from core.database.connection import get_session
from core.models.app_account import AppLoginToken
from core.models.employee import Employee
from core.models.tenant import Tenant

# Grosszuegige Gueltigkeit fuer den manuellen Test (1 Stunde).
LINK_TTL = dt.timedelta(hours=1)


async def _list() -> None:
    async with get_session() as s:
        tenants = (await s.execute(select(Tenant).order_by(Tenant.slug))).scalars().all()
        print(f"{'SLUG':<20} {'FIRMA':<34} INHABER")
        print("-" * 72)
        for t in tenants:
            e = (await s.execute(select(Employee).where(
                Employee.tenant_id == t.id, Employee.is_default.is_(True)
            ))).scalar_one_or_none()
            who = (f"{e.name} (aktiv={e.is_active})" if e else "(kein Inhaber-Account)")
            print(f"{t.slug:<20} {(t.company_name or '')[:34]:<34} {who}")


async def _make(slug: str) -> None:
    async with get_session() as s:
        t = (await s.execute(select(Tenant).where(Tenant.slug == slug))).scalar_one_or_none()
        if not t:
            print(f"FEHLER: Betrieb '{slug}' nicht gefunden. Mit --list pruefen."); return
        e = (await s.execute(select(Employee).where(
            Employee.tenant_id == t.id, Employee.is_default.is_(True)
        ))).scalar_one_or_none()
        if not e:
            print(f"FEHLER: kein Inhaber-Account (is_default) fuer '{slug}'"); return
        if not e.is_active:
            e.is_active = True  # sonst lehnt consume_login_token den Token ab
            print(f"HINWEIS: Inhaber-Account war inaktiv -> aktiviert.")
        now = dt.datetime.now(dt.timezone.utc)
        token = secrets.token_urlsafe(40)
        s.add(AppLoginToken(
            employee_id=e.id, tenant_id=t.id, token=token,
            expires_at=now + LINK_TTL,
        ))
    base = (settings.app_base_url or settings.public_url).rstrip("/")
    print(f"\nLOGIN-LINK fuer {slug}/{e.name} (1 Stunde gueltig, einmalig):\n")
    print(f"  {base}/app/login/{token}\n")
    print("Auf dem Handy oeffnen -> direkt eingeloggt.")


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] in ("--list", "-l"):
        asyncio.run(_list()); return
    asyncio.run(_make(args[0]))


if __name__ == "__main__":
    main()
