"""Testet das Plugin-System End-to-End mit dem Hello-Plugin."""
import asyncio

from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.models import Tenant, ToolConfig
from core.plugin_system import discover_plugins, get_plugin_for_tenant


async def main() -> None:
    # 1. Plugins entdecken
    discover_plugins()
    print("Discovered plugins:")
    from core.plugin_system import PLUGIN_MANIFESTS
    for name, manifest in PLUGIN_MANIFESTS.items():
        print(f"  - {name} v{manifest.version}: {manifest.display_name}")

    # 2. Hello-Plugin fuer Tenant dietz aktivieren (falls nicht schon)
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Tenant).where(Tenant.slug == "dietz"))
        dietz = result.scalar_one()

        result = await session.execute(
            select(ToolConfig).where(
                ToolConfig.tenant_id == dietz.id,
                ToolConfig.tool_name == "hello",
            )
        )
        tc = result.scalar_one_or_none()
        if not tc:
            tc = ToolConfig(
                tenant_id=dietz.id,
                tool_name="hello",
                enabled=True,
                config={"greeting": "Moin"},
            )
            session.add(tc)
            await session.commit()
            print(f"\nHello-Plugin fuer Dietz aktiviert")
        else:
            print(f"\nHello-Plugin war schon aktiviert")

    # 3. Plugin-Instance fuer Dietz holen
    plugin = await get_plugin_for_tenant("dietz", "hello")
    if plugin is None:
        print("FEHLER: Plugin nicht geladen")
        return

    # 4. Webhook simulieren
    response = await plugin.on_webhook("greet", {"name": "Fabian"})
    print(f"\nWebhook-Response: {response}")


if __name__ == "__main__":
    asyncio.run(main())
