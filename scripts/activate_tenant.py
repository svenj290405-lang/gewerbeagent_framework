"""Setzt Tenant-Status auf ACTIVE nach erfolgreichem Onboarding."""
from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.models import Tenant, TenantStatus


async def activate(slug: str) -> None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Tenant).where(Tenant.slug == slug)
        )
        tenant = result.scalar_one_or_none()

        if not tenant:
            print(f"FEHLER: Kein Tenant mit Slug '{slug}' gefunden.")
            sys.exit(1)

        if tenant.status == TenantStatus.ACTIVE:
            print(f"Tenant '{slug}' ist bereits ACTIVE.")
            return

        old_status = tenant.status.value if hasattr(tenant.status, "value") else tenant.status
        tenant.status = TenantStatus.ACTIVE
        await session.commit()

        print(f"Tenant '{slug}' ({tenant.company_name}) aktiviert.")
        print(f"  Status: {old_status} -> active")


def main() -> None:
    parser = argparse.ArgumentParser(description="Aktiviert einen Tenant.")
    parser.add_argument("--slug", required=True)
    args = parser.parse_args()
    asyncio.run(activate(args.slug))


if __name__ == "__main__":
    main()
