"""Prueft, ob ein Microsoft-OAuth-Token noch refreshbar ist.

Hintergrund: das Plattform-Postfach `_global` (Health-Mail, Onboarding-Mail)
wurde seit Monaten nicht benutzt. Microsoft entwertet Refresh-Tokens nach
90 Tagen Inaktivitaet — dieser Probe erzwingt einen Refresh und zeigt, ob
der Weg noch lebt, BEVOR er im Ernstfall gebraucht wird.

Der Refresh rotiert den Token (gewollter Seiteneffekt: die Uhr faengt von
vorn an). Es werden keine Token-Werte ausgegeben.

  docker compose exec -T -e PYTHONPATH=/app framework \\
      .venv/bin/python scripts/probe_global_token.py [tenant-slug ...]
"""
from __future__ import annotations

import asyncio
import datetime as dt
import sys

from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.models import Tenant
from core.models.employee import get_default_employee
from core.security.oauth_token_lookup import find_oauth_token


async def pruefe(slug: str) -> bool:
    async with AsyncSessionLocal() as s:
        tenant = (await s.execute(
            select(Tenant).where(Tenant.slug == slug)
        )).scalar_one_or_none()
        if tenant is None:
            print(f"  {slug}: kein Tenant")
            return False
        s.expunge(tenant)

    emp = await get_default_employee(tenant.id)
    token = await find_oauth_token(tenant.id, "microsoft", emp.id if emp else None)
    if token is None:
        print(f"  {slug}: kein Microsoft-Token")
        return False

    alt = token.access_token_expires_at
    print(f"  {slug}: Token gefunden, access_token_expires_at={alt}")

    from core.integrations.microsoft import _refresh_access_token
    try:
        _, neu = await _refresh_access_token(token)
    except Exception as exc:  # noqa: BLE001
        print(f"  {slug}: ❌ Refresh FEHLGESCHLAGEN — {type(exc).__name__}: "
              f"{str(exc)[:200]}")
        return False

    jetzt = dt.datetime.now(dt.timezone.utc)
    rest = (neu - jetzt).total_seconds() / 60 if neu else 0
    print(f"  {slug}: ✅ Refresh ok, neue Gueltigkeit bis {neu} "
          f"({rest:.0f} Min)")
    return True


async def main() -> int:
    slugs = sys.argv[1:] or ["_global"]
    print("=" * 68)
    print("Microsoft-Token-Probe (erzwingt Refresh)")
    print("=" * 68)
    ok = True
    for slug in slugs:
        ok = await pruefe(slug) and ok
    print("=" * 68)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
