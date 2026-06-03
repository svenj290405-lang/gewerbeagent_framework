"""Login-Mail fuer die PWA freischalten.

Der App-Login (Magic-Link) sucht einen Employee mit passender
``contact_email``. Dieses Skript setzt diese Mail auf dem Inhaber-Account
(is_default) eines Betriebs — danach kann sich derjenige unter
https://gewerbeagent.de/app einloggen.

Nutzung (im Prod-Container):
  # 1) Erst anschauen, welche Betriebe + aktuelle Inhaber-Mails es gibt:
  docker exec gewerbeagent_framework /app/.venv/bin/python \
      /app/scripts/set_login_email.py --list

  # 2) Mail auf einem Betrieb setzen:
  docker exec gewerbeagent_framework /app/.venv/bin/python \
      /app/scripts/set_login_email.py <email> <tenant_slug>
"""
from __future__ import annotations

import asyncio
import sys

from sqlalchemy import func, select

from core.database.connection import get_session
from core.models.employee import Employee
from core.models.tenant import Tenant


async def _list() -> None:
    async with get_session() as s:
        tenants = (await s.execute(select(Tenant).order_by(Tenant.slug))).scalars().all()
        print(f"{'SLUG':<20} {'FIRMA':<32} INHABER-MAIL")
        print("-" * 78)
        for t in tenants:
            e = (await s.execute(select(Employee).where(
                Employee.tenant_id == t.id, Employee.is_default.is_(True)
            ))).scalar_one_or_none()
            mail = (e.contact_email if e else None) or "—"
            name = (e.name if e else "(kein Default-Employee)")
            print(f"{t.slug:<20} {(t.company_name or '')[:32]:<32} {mail}  [{name}]")


async def _set(email: str, slug: str) -> None:
    email = email.strip()
    if "@" not in email:
        print(f"FEHLER: '{email}' ist keine Mailadresse"); return
    async with get_session() as s:
        dup = (await s.execute(select(Tenant.slug, Employee.slug)
                               .join(Employee, Employee.tenant_id == Tenant.id)
                               .where(func.lower(Employee.contact_email) == email.lower())
                               )).all()
        if dup:
            print(f"HINWEIS: Mail ist bereits gesetzt bei: {list(dup)} "
                  f"(Login waehlt bei Mehrfach-Treffern einen davon).")
        t = (await s.execute(select(Tenant).where(Tenant.slug == slug))).scalar_one_or_none()
        if not t:
            print(f"FEHLER: Betrieb '{slug}' nicht gefunden. Mit --list pruefen."); return
        e = (await s.execute(select(Employee).where(
            Employee.tenant_id == t.id, Employee.is_default.is_(True)
        ))).scalar_one_or_none()
        if not e:
            print(f"FEHLER: kein Inhaber-Account (is_default) fuer '{slug}'"); return
        print(f"VORHER:  {slug}/{e.slug}  name={e.name!r}  "
              f"email={e.contact_email!r}  aktiv={e.is_active}")
        e.contact_email = email
        e.is_active = True
    print(f"OK: Login freigeschaltet — {slug}/{e.slug}.contact_email = {email} (aktiv).")
    print("    -> https://gewerbeagent.de/app  (Mail eingeben, Login-Link kommt per Mail)")


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] in ("--list", "-l"):
        asyncio.run(_list()); return
    if len(args) != 2:
        print("Nutzung: set_login_email.py <email> <tenant_slug>   |   --list")
        sys.exit(1)
    asyncio.run(_set(args[0], args[1]))


if __name__ == "__main__":
    main()
