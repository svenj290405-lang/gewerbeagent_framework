"""Per-Tenant-Feature-Toggle-Helpers.

Eine kleine Schicht oben auf `tool_configs`. Liest und schreibt
ToolConfig.enabled per Feature-Key, mit kurzem In-Memory-Cache damit
der Telegram-Bot nicht pro Update 10x DB-Roundtrips macht.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass

from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.models import ToolConfig
from core.features.catalog import (
    FEATURES,
    PACKAGES,
    PACKAGE_BASIS,
    PACKAGE_PRO,
    PACKAGE_ENTERPRISE,
    PACKAGE_CUSTOM,
    features_in_package,
)

logger = logging.getLogger(__name__)


# =====================================================================
# In-Process-Cache (TTL: 60s)
# =====================================================================
# Telegram-Updates kommen alle 1-3s — ohne Cache wuerde jeder /help
# 10 DB-Calls (einer pro Befehl-Filter) ausloesen. 60s-TTL ist OK weil
# Feature-Toggle fast nie passiert; bei Toggle ruft das Admin-UI
# invalidate_feature_cache(tenant_id) auf damit User die Aenderung
# direkt sehen.

_CACHE_TTL_SECONDS = 60


@dataclass
class _CacheEntry:
    enabled_set: frozenset[str]
    expires_at: float


_cache: dict[uuid.UUID, _CacheEntry] = {}


def invalidate_feature_cache(tenant_id: uuid.UUID | None = None) -> None:
    """Leert den Cache (nach Admin-Toggle oder /paket-Aenderung).

    None -> kompletter Cache wird geleert (z.B. bei Boot-Strap).
    """
    if tenant_id is None:
        _cache.clear()
    else:
        _cache.pop(tenant_id, None)


# =====================================================================
# Public API
# =====================================================================

# Kill-Switch: hier gelistete Features sind fuer ALLE Tenants aus —
# unabhaengig von ToolConfig/Paket. Code + DB-Felder bleiben dormant
# (reversibel: einfach aus der Menge entfernen). 'werkstatt' (Smart-
# Routing ueber Heimat-Adresse) wird momentan nicht benoetigt; die
# Anschrift wird weiterhin im Onboarding fuers Impressum erfasst.
GLOBALLY_DISABLED_FEATURES: frozenset[str] = frozenset({"werkstatt"})


async def enabled_features_for_tenant(
    tenant_id: uuid.UUID,
) -> frozenset[str]:
    """Liefert das Set der aktivierten Features fuer einen Tenant.

    Always-on-Features sind IMMER drin, unabhaengig von ToolConfig.
    """
    entry = _cache.get(tenant_id)
    now = time.monotonic()
    if entry is not None and entry.expires_at > now:
        return entry.enabled_set

    always_on = frozenset(
        f.key for f in FEATURES.values() if f.always_on
    )

    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(ToolConfig.tool_name)
            .where(ToolConfig.tenant_id == tenant_id)
            .where(ToolConfig.enabled.is_(True))
        )).all()

    enabled = (
        (frozenset(r[0] for r in rows) | always_on)
        - GLOBALLY_DISABLED_FEATURES
    )
    _cache[tenant_id] = _CacheEntry(
        enabled_set=enabled,
        expires_at=now + _CACHE_TTL_SECONDS,
    )
    return enabled


async def is_feature_enabled(
    tenant_id: uuid.UUID,
    feature_key: str,
) -> bool:
    """Schnell-Check: ist Feature `feature_key` fuer Tenant aktiv?

    Wenn `feature_key` nicht im Catalog ist → False (sicher).
    """
    if feature_key not in FEATURES:
        # Unbekanntes Feature -> deny by default. Verhindert dass
        # Tippfehler im Code zu silent-pass fuehren.
        logger.warning(
            "is_feature_enabled: unbekannter Feature-Key '%s'", feature_key,
        )
        return False
    enabled = await enabled_features_for_tenant(tenant_id)
    return feature_key in enabled


async def apply_package(
    tenant_id: uuid.UUID,
    package: str,
) -> None:
    """Setzt ToolConfig.enabled gemaess Paket-Definition.

    Idempotent. Features im Paket -> enabled=True, Features im Catalog
    aber NICHT im Paket -> enabled=False. Always-on-Features werden
    nicht in tool_configs geschrieben (sie sind per Definition aktiv,
    egal was in der DB steht).

    PACKAGE_CUSTOM ist no-op (Sven setzt Features einzeln).

    Konfigurations-Daten existierender ToolConfig-Eintraege bleiben
    erhalten — wir aendern nur das `enabled`-Flag.
    """
    if package == PACKAGE_CUSTOM:
        logger.info("apply_package(%s, custom): no-op", tenant_id)
        return

    target_features = features_in_package(package)
    if not target_features:
        raise ValueError(f"Paket '{package}' nicht im Catalog")

    # Always-on-Features ueberspringen — sie haben keinen Toggle in der
    # ToolConfig-Tabelle (oder wenn doch, lassen wir das enabled-Flag
    # auf True).
    target_keys_db = {
        f.key for f in FEATURES.values()
        if f.key in target_features and not f.always_on
    }
    catalog_keys_db = {
        f.key for f in FEATURES.values() if not f.always_on
    }

    async with AsyncSessionLocal() as session:
        # Bestehende ToolConfig-Zeilen laden
        rows = (await session.execute(
            select(ToolConfig)
            .where(ToolConfig.tenant_id == tenant_id)
        )).scalars().all()
        existing_by_name = {tc.tool_name: tc for tc in rows}

        # Fuer jedes Catalog-Feature: setzen oder anlegen
        for key in catalog_keys_db:
            should_enable = key in target_keys_db
            tc = existing_by_name.get(key)
            if tc is None:
                # Anlegen mit enabled=should_enable
                tc = ToolConfig(
                    tenant_id=tenant_id,
                    tool_name=key,
                    enabled=should_enable,
                    config={},
                )
                session.add(tc)
            else:
                if tc.enabled != should_enable:
                    tc.enabled = should_enable

        await session.commit()

    invalidate_feature_cache(tenant_id)
    logger.info(
        "apply_package(%s, %s): %d Features aktiviert, %d deaktiviert",
        tenant_id, package, len(target_keys_db),
        len(catalog_keys_db) - len(target_keys_db),
    )


def detect_package_from_features(
    enabled: frozenset[str],
) -> str:
    """Versucht aus dem aktiven Feature-Set ein Paket zu erkennen.

    Verwendet fuer Tenant.package_tier-Auto-Setting im Backfill +
    Admin-UI-Anzeige. Liefert PACKAGE_CUSTOM wenn die Menge mit keinem
    vordefinierten Paket exakt uebereinstimmt.

    Beim Vergleich:
    - Always-on-Features werden ignoriert (sind per Definition immer aktiv)
    - tool_configs.tool_name-Werte die NICHT im Catalog sind werden
      ignoriert (z.B. legacy 'microsoft_oauth', 'telegram_bot' am
      _global-Tenant — das sind Infra-Configs, keine Features).
    """
    always_on = frozenset(
        f.key for f in FEATURES.values() if f.always_on
    )
    catalog_keys = frozenset(FEATURES.keys())
    # Nur Catalog-Features minus always_on betrachten — alles andere
    # ist legacy/infra und stoert das Mapping.
    relevant = (enabled & catalog_keys) - always_on

    for pkg_name in (PACKAGE_BASIS, PACKAGE_PRO, PACKAGE_ENTERPRISE):
        pkg_features = PACKAGES[pkg_name]
        if relevant == pkg_features:
            return pkg_name
    return PACKAGE_CUSTOM
