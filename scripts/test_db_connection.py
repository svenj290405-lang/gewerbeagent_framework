"""Testet ob die Postgres-Verbindung klappt."""
import asyncio

from sqlalchemy import text

from core.database import engine


async def main() -> None:
    async with engine.begin() as conn:
        result = await conn.execute(text("SELECT version()"))
        version = result.scalar()
        print("Postgres-Verbindung OK")
        print(f"  Server: {version}")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
