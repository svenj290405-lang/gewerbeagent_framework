"""Ein Feature fuer einen Betrieb ein- oder ausschalten.

    docker exec -w /app -e PYTHONPATH=/app gewerbeagent_framework \
        /app/.venv/bin/python scripts/enable_feature.py <slug> <feature> [aus]

Legt die ToolConfig-Zeile an, wenn es noch keine gibt. Der Admin-Bereich
kann das auch, aber fuer neu eingefuehrte Features ist ein Einzeiler
schneller als der Weg durch die Oberflaeche.
"""
import asyncio
import sys

from sqlalchemy import select

from core.database.connection import get_session
from core.features.catalog import FEATURES
from core.models import ToolConfig
from core.models.tenant import Tenant


async def main(slug: str, feature: str, an: bool) -> None:
    if feature not in FEATURES:
        print(f"Unbekanntes Feature {feature!r}. Bekannt: {', '.join(sorted(FEATURES))}")
        return
    async with get_session() as s:
        t = (await s.execute(
            select(Tenant).where(Tenant.slug == slug))).scalar_one_or_none()
        if t is None:
            print(f"Betrieb {slug!r} nicht gefunden.")
            return
        tc = (await s.execute(
            select(ToolConfig).where(ToolConfig.tenant_id == t.id,
                                     ToolConfig.tool_name == feature)
        )).scalar_one_or_none()
        if tc is None:
            tc = ToolConfig(tenant_id=t.id, tool_name=feature, enabled=an, config={})
            s.add(tc)
            print(f"ToolConfig angelegt: {feature} = {an}")
        else:
            tc.enabled = an
            print(f"ToolConfig gesetzt: {feature} = {an}")
        await s.commit()

    from core.features.check import invalidate_feature_cache
    invalidate_feature_cache(t.id)
    print(f"{t.company_name} ({slug}): {feature} ist jetzt {'AN' if an else 'AUS'}.")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        raise SystemExit(1)
    asyncio.run(main(sys.argv[1], sys.argv[2], (sys.argv[3:4] or [""])[0] != "aus"))
