"""Backfill-Skript fuer das Paket-System.

Einmalig nach Migration u9v5w7x4y8z2 ausfuehren um:
1. Pro existierenden Tenant: aktuelles Feature-Set lesen
   (tool_configs.enabled=True)
2. Mit detect_package_from_features das passende Paket erkennen
3. Tenant.package_tier setzen (basis/pro/enterprise/custom)
4. Fehlende ToolConfig-Zeilen fuer alle Catalog-Features mit dem
   passenden enabled-Wert anlegen — damit das Admin-UI fuer jeden
   Toggle einen klaren Datensatz hat.

Idempotent — beliebig oft laufbar:
- Bestehende ToolConfig.enabled=False werden NICHT auf True gesetzt
  (Schutz fuer manuell deaktivierte Features bei Sven-Test-Tenants).
- Bestehende ToolConfig.config bleibt unveraendert.
- Tenant.package_tier wird nur ueberschrieben wenn das aktuelle
  Feature-Set EXAKT einem Paket entspricht — sonst bleibt es bei
  'pro' (Default aus der Migration).

Verwendung:
    docker compose -p prod -f docker-compose.prod.yml exec framework \\
        uv run python -m scripts.backfill_tenant_features

    docker compose -p dev -f docker-compose.dev.yml exec framework_dev \\
        uv run python -m scripts.backfill_tenant_features

Optional --dry-run: zeigt was getan wuerde ohne zu schreiben.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.features.catalog import (
    FEATURES,
    PACKAGE_PRO,
    features_in_package,
)
from core.features.check import detect_package_from_features
from core.models import Tenant, ToolConfig


async def backfill(dry_run: bool = False) -> None:
    """Iteriert ueber alle Tenants und gleicht ToolConfig + package_tier ab."""

    # Catalog-Features die in tool_configs auftauchen sollen (alle nicht
    # always_on — die haben keinen Toggle).
    catalog_keys = {f.key for f in FEATURES.values() if not f.always_on}

    async with AsyncSessionLocal() as session:
        tenants = (await session.execute(select(Tenant))).scalars().all()

        if not tenants:
            print("Keine Tenants gefunden — nichts zu tun.")
            return

        print(f"Gefunden: {len(tenants)} Tenant(s)")
        print()

        changes_count = 0

        for tenant in tenants:
            print(f"=== Tenant '{tenant.slug}' ({tenant.company_name}) ===")

            # 1. Aktuelles Feature-Set lesen
            tcs = (await session.execute(
                select(ToolConfig)
                .where(ToolConfig.tenant_id == tenant.id)
            )).scalars().all()
            existing = {tc.tool_name: tc for tc in tcs}
            enabled_now = frozenset(
                tc.tool_name for tc in tcs if tc.enabled
            )

            # 2. Paket erkennen
            detected_pkg = detect_package_from_features(enabled_now)
            print(f"  Aktuelles Set: {sorted(enabled_now)}")
            print(f"  Erkanntes Paket: {detected_pkg}")
            print(f"  Tenant.package_tier vorher: {tenant.package_tier}")

            # 3. package_tier nur dann ueberschreiben wenn detected_pkg
            #    nicht 'custom' ist UND von 'pro' (Migration-Default)
            #    abweicht
            if detected_pkg != "custom" and tenant.package_tier != detected_pkg:
                print(f"  -> tier setzen: {tenant.package_tier} -> {detected_pkg}")
                if not dry_run:
                    tenant.package_tier = detected_pkg
                changes_count += 1

            # 4. Fehlende ToolConfig-Zeilen anlegen (entsprechend
            #    aktuellem package_tier — ggf. das eben aktualisierte)
            target_tier = (
                detected_pkg if detected_pkg != "custom"
                else tenant.package_tier
            )
            target_features = features_in_package(target_tier)

            missing = catalog_keys - set(existing.keys())
            if missing:
                print(f"  Fehlende ToolConfig-Zeilen ({len(missing)}):")
                for key in sorted(missing):
                    should_enable = key in target_features
                    print(f"    + {key} (enabled={should_enable})")
                    if not dry_run:
                        session.add(ToolConfig(
                            tenant_id=tenant.id,
                            tool_name=key,
                            enabled=should_enable,
                            config={},
                        ))
                changes_count += 1
            else:
                print(f"  Alle {len(catalog_keys)} Catalog-Features haben ToolConfig — OK")

            print()

        if not dry_run and changes_count > 0:
            await session.commit()
            print(f"✅ {changes_count} Aenderung(en) committed")
        elif dry_run:
            print(f"DRY-RUN: {changes_count} Aenderungen waeren ausgefuehrt worden")
        else:
            print("Nichts zu tun — alle Tenants schon konsistent")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill: Tenant.package_tier + fehlende ToolConfig-Zeilen.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Nur anzeigen was getan wuerde, nichts schreiben",
    )
    args = parser.parse_args()

    asyncio.run(backfill(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
