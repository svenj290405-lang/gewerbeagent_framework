"""Listet alle Tenants mit ihrem Status und aktiven Plugins."""
import asyncio

from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.models import Tenant


async def main() -> None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Tenant).order_by(Tenant.created_at))
        tenants = result.scalars().all()

        if not tenants:
            print("Keine Tenants vorhanden.")
            return

        print()
        print(f"{'Slug':<15} {'Firma':<30} {'Status':<12} {'Plugins aktiv'}")
        print("-" * 80)
        for t in tenants:
            status_str = t.status.value if hasattr(t.status, "value") else t.status
            active_plugins = [c.tool_name for c in t.tool_configs if c.enabled]
            plugins_str = ", ".join(active_plugins) if active_plugins else "-"
            print(f"{t.slug:<15} {t.company_name:<30} {status_str:<12} {plugins_str}")


if __name__ == "__main__":
    asyncio.run(main())
