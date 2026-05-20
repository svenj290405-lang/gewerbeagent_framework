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
from core.features.catalog import FEATURES

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
    """Leert den Cache (nach Feature-Toggle im Admin-UI).

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
# unabhaengig von ToolConfig. Code + DB-Felder bleiben dormant
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
