"""
Schaltet Features fuer einen BESTEHENDEN Tenant frei.

Nutzung:
  uv run python -m scripts.enable_features --slug pilot --all
  uv run python -m scripts.enable_features --slug pilot --features drive_archiv,voice_init

--all       aktiviert ALLE umschaltbaren Tools (ausser always-on-Features
            und global per Kill-Switch deaktivierten wie 'werkstatt').
--features  Kommagetrennte Liste einzelner Feature-Keys.

Legt fehlende ToolConfigs an bzw. setzt enabled=True. Greift im laufenden
Bot nach <=60s (Feature-Cache-TTL) — kein Neustart noetig. Fuer NEUE
Betriebe regelt das bereits DEFAULT_FEATURES in scripts/onboard.py.
"""
from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.models import Tenant, ToolConfig
from core.features.catalog import FEATURES
from core.features.check import GLOBALLY_DISABLED_FEATURES, invalidate_feature_cache


def _all_toggleable() -> list[str]:
    """Alle umschaltbaren Tools: nicht always-on, nicht global deaktiviert."""
    return [
        key for key, feat in FEATURES.items()
        if not getattr(feat, "always_on", False)
        and key not in GLOBALLY_DISABLED_FEATURES
    ]


async def enable_features(slug: str, keys: list[str]) -> None:
    async with AsyncSessionLocal() as session:
        tenant = (await session.execute(
            select(Tenant).where(Tenant.slug == slug)
        )).scalar_one_or_none()
        if tenant is None:
            print(f"FEHLER: Kein Tenant mit Slug '{slug}' gefunden.")
            raise SystemExit(1)
        tenant_id = tenant.id
        existing = {
            tc.tool_name: tc
            for tc in (await session.execute(
                select(ToolConfig).where(ToolConfig.tenant_id == tenant_id)
            )).scalars().all()
        }
        report: list[str] = []
        for key in keys:
            if key not in FEATURES:
                report.append(f"  ! {key}: unbekanntes Feature — uebersprungen")
                continue
            if key in GLOBALLY_DISABLED_FEATURES:
                report.append(f"  - {key}: global deaktiviert (Kill-Switch) — uebersprungen")
                continue
            tc = existing.get(key)
            if tc is None:
                session.add(ToolConfig(
                    tenant_id=tenant_id, tool_name=key, enabled=True, config={},
                ))
                report.append(f"  + {key}: angelegt + aktiviert")
            elif not tc.enabled:
                tc.enabled = True
                report.append(f"  + {key}: aktiviert")
            else:
                report.append(f"  = {key}: war schon aktiv")
        await session.commit()

    # Cache des LAUFENDEN Bots ist ein eigener Prozess — dieser Aufruf
    # leert nur den (leeren) Skript-Cache. Im Bot greift die Aenderung
    # ueber die 60s-TTL. Aufruf bleibt fuer den In-Process-Fall korrekt.
    invalidate_feature_cache(tenant_id)

    print(f"Tenant '{slug}':")
    for line in report:
        print(line)
    print("Fertig. Im laufenden Bot greift es nach <=60s (Cache-TTL).")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Features fuer einen bestehenden Tenant freischalten.",
    )
    parser.add_argument("--slug", required=True, help="Slug des Tenants")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--all", action="store_true",
        help="Alle umschaltbaren Tools aktivieren",
    )
    group.add_argument(
        "--features",
        help="Kommagetrennte Feature-Keys (z.B. drive_archiv,voice_init)",
    )
    args = parser.parse_args()
    if args.all:
        keys = _all_toggleable()
    else:
        keys = [k.strip() for k in args.features.split(",") if k.strip()]
    asyncio.run(enable_features(slug=args.slug, keys=keys))


if __name__ == "__main__":
    main()
