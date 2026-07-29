"""Generiert einen Magic-Link (App-Login-Token) fuer einen Employee.

Aufruf im Container:
  python scripts/gen_magic_link.py <tenant_slug> [employee_email_or_name]

Ohne 2. Argument: listet nur die Employees des Tenants.
"""
import asyncio
import datetime as dt
import secrets
import sys

from core.plugins import discover_plugins
from core.database.connection import get_session
from config.settings import settings
from sqlalchemy import select


async def main() -> None:
    discover_plugins()
    from core.models.tenant import Tenant
    from core.models.employee import Employee
    from core.models.app_account import AppLoginToken, APP_LOGIN_TOKEN_LIFETIME

    slug = sys.argv[1] if len(sys.argv) > 1 else "pilot"
    needle = sys.argv[2] if len(sys.argv) > 2 else None

    async with get_session() as s:
        t = (await s.execute(select(Tenant).where(Tenant.slug == slug))).scalar_one_or_none()
        if not t:
            print(f"Kein Tenant mit slug={slug!r}")
            return
        emps = (await s.execute(
            select(Employee).where(Employee.tenant_id == t.id)
        )).scalars().all()

        if not needle:
            print(f"Employees fuer Tenant {slug!r} ({t.id}):")
            for e in emps:
                print(f"  - {e.name!r}  email={e.contact_email!r}  active={e.is_active}")
            print("\nNochmal mit Email/Name als 2. Argument aufrufen, um Link zu erzeugen.")
            return

        match = [e for e in emps if needle.lower() in (e.name or "").lower()
                 or needle.lower() in (e.contact_email or "").lower()]
        if not match:
            print(f"Kein Employee passend zu {needle!r}. Verfuegbar:")
            for e in emps:
                print(f"  - {e.name!r}  email={e.contact_email!r}")
            return
        e = match[0]
        now = dt.datetime.now(dt.timezone.utc)
        tok = AppLoginToken(
            employee_id=e.id,
            tenant_id=e.tenant_id,
            token=secrets.token_urlsafe(40),
            expires_at=now + APP_LOGIN_TOKEN_LIFETIME,
            ip_address=None,
        )
        s.add(tok)
        await s.flush()
        await s.commit()
        link = f"{settings.app_url}/app/login/{tok.token}"
        print(f"Employee: {e.name!r} ({e.contact_email})")
        print(f"Gueltig bis: {tok.expires_at.isoformat()} (20 Min)")
        print(f"\nMAGIC-LINK:\n{link}")


asyncio.run(main())
