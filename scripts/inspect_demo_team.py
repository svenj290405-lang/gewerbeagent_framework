"""READ-ONLY: Ist-Zustand des demo-Tenants fuer den Krank-E2E-Test.

Zeigt pro Mitarbeiter: slug, name, default/aktiv, telegram_chat_id,
calendar_provider/-id, skills, arbeitstage/-zeiten. Plus: ist das
Feature 'mitarbeiter' aktiv? Aendert NICHTS.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.models import Tenant, Employee
from core.models.tool_config import ToolConfig


async def main():
    async with AsyncSessionLocal() as s:
        all_tenants = (await s.execute(select(Tenant))).scalars().all()
        print(f"Alle Tenants ({len(all_tenants)}): "
              + ", ".join(f"{x.slug}({x.id})" for x in all_tenants))
        t = next((x for x in all_tenants if x.slug == "demo"), None)
        if t is None:
            t = next((x for x in all_tenants if x.slug != "_global"), None)
        if t is None:
            print("KEIN nutzbarer Tenant gefunden.")
            return
        print(f"\n>>> Inspiziere Tenant: {t.slug}")
        print(f"Tenant: slug={t.slug} id={t.id}")
        print(f"  tenant.telegram_chat_id (legacy) = {getattr(t, 'telegram_chat_id', None)}")

        emps = (await s.execute(
            select(Employee).where(Employee.tenant_id == t.id)
        )).scalars().all()
        print(f"\n{len(emps)} Mitarbeiter:")
        for e in emps:
            print(
                f"  - slug={e.slug!r} name={e.name!r} "
                f"default={e.is_default} aktiv={e.is_active}\n"
                f"      chat_id={e.telegram_chat_id} "
                f"cal_provider={e.calendar_provider!r} cal_id={e.calendar_id!r}\n"
                f"      skills={e.skills} arbeitstage={e.arbeitstage} "
                f"arbeitszeiten={e.arbeitszeiten}"
            )

        tcs = (await s.execute(
            select(ToolConfig).where(ToolConfig.tenant_id == t.id)
        )).scalars().all()
        print("\nTool-Configs (enabled):")
        for tc in sorted(tcs, key=lambda x: x.tool_name):
            print(f"  - {tc.tool_name}: enabled={tc.enabled}")


if __name__ == "__main__":
    asyncio.run(main())
