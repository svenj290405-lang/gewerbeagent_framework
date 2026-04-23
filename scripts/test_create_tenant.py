"""Legt einen Test-Tenant in der DB an und liest ihn zurueck."""
import asyncio

from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.models import Tenant, ToolConfig


async def main() -> None:
    async with AsyncSessionLocal() as session:
        # Tenant anlegen
        dietz = Tenant(
            slug="dietz",
            company_name="Tischlerei Dietz",
            contact_name="Fabian Dietz",
            contact_email="f.dietz@pura-tischler.de",
            contact_phone="+49 xxx",
            notes="Pilotkunde 1, Demo am 24.04.2026",
        )

        # Gleichzeitig ein Tool fuer ihn aktivieren
        kalender_config = ToolConfig(
            tool_name="kalender",
            enabled=True,
            config={"arbeitszeiten_start": "08:00", "arbeitszeiten_ende": "17:00"},
        )
        dietz.tool_configs.append(kalender_config)

        session.add(dietz)
        await session.commit()
        await session.refresh(dietz)

        print(f"Tenant angelegt: {dietz}")
        print(f"  ID: {dietz.id}")
        print(f"  Status: {dietz.status}")
        print(f"  Tools: {[(t.tool_name, t.enabled) for t in dietz.tool_configs]}")

    # Zweite Session: zuruecklesen um zu beweisen dass es persistiert
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Tenant).where(Tenant.slug == "dietz")
        )
        dietz_from_db = result.scalar_one()

        print(f"\nAus DB gelesen: {dietz_from_db}")
        print(f"  Tools: {[(t.tool_name, t.config) for t in dietz_from_db.tool_configs]}")


if __name__ == "__main__":
    asyncio.run(main())
