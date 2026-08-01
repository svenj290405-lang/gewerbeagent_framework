"""Lesen und Schreiben des Automatisierungsgrads pro Tenant.

Duenne Schicht ueber ``automation_settings``, gebaut wie
``core/features/check.py``: derselbe 60s-In-Process-Cache, dieselbe
Invalidierung nach dem Schreiben. Der Q-Chat fragt die Stufe bei jedem
Befehl ab — ohne Cache waere das ein DB-Roundtrip pro Nachricht.

Fehlt eine Zeile in der DB, gilt ``Automation.default_mode``. Es gibt also
keinen Zustand "nicht konfiguriert", den Aufrufer behandeln muessten.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass

from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.features.automations import (
    AUTOMATIONS,
    MODE_ASSISTIERT,
    MODE_AUTOMATISCH,
    MODE_MANUELL,
    automation_for_tool,
    default_modes,
    is_valid_mode,
)

logger = logging.getLogger(__name__)


_CACHE_TTL_SECONDS = 60


@dataclass
class _CacheEntry:
    modes: dict[str, str]
    expires_at: float


_cache: dict[uuid.UUID, _CacheEntry] = {}


def invalidate_automation_cache(tenant_id: uuid.UUID | None = None) -> None:
    """Leert den Cache (nach dem Speichern in den Einstellungen).

    None -> kompletter Cache (z.B. im Test).
    """
    if tenant_id is None:
        _cache.clear()
    else:
        _cache.pop(tenant_id, None)


# =====================================================================
# Lesen
# =====================================================================


async def automation_modes_for_tenant(
    tenant_id: uuid.UUID,
) -> dict[str, str]:
    """Alle Automatisierungen dieses Tenants mit ihrer aktuellen Stufe.

    Immer vollstaendig: jeder Key aus AUTOMATIONS ist enthalten, gespeicherte
    Zeilen ueberschreiben nur die Defaults.
    """
    entry = _cache.get(tenant_id)
    now = time.monotonic()
    if entry is not None and entry.expires_at > now:
        return dict(entry.modes)

    from core.models import AutomationSetting

    modes = default_modes()
    try:
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(
                select(AutomationSetting.automation_key, AutomationSetting.mode)
                .where(AutomationSetting.tenant_id == tenant_id)
            )).all()
    except Exception as exc:  # noqa: BLE001
        # Dieser Lesepfad haengt am Telefon-Webhook: ein DB-Fehler darf den
        # laufenden Anruf nicht abbrechen. Zwei Faelle:
        #   - Tabelle fehlt noch (Code deployed, Migration noch nicht) —
        #     dann hat auch niemand etwas eingestellt, und die Defaults sind
        #     exakt das Verhalten von vorher.
        #   - Transienter Fehler — dann ist die zuletzt gelesene Einstellung
        #     naeher an der Wahrheit als der Default, also nehmen wir den
        #     abgelaufenen Cache-Eintrag statt ihn zu verwerfen.
        logger.warning(
            "automation_settings nicht lesbar (tenant=%s): %s — "
            "nutze %s", tenant_id, exc,
            "letzten bekannten Stand" if entry else "Defaults",
        )
        return dict(entry.modes) if entry else modes

    for key, mode in rows:
        # Zeile aus einer alten Version, deren Automatisierung es nicht mehr
        # gibt, oder eine Stufe die die Automatisierung nicht mehr kann:
        # ignorieren statt crashen — der Default greift.
        if key not in AUTOMATIONS:
            continue
        if not is_valid_mode(key, mode):
            logger.warning(
                "automation_settings: Stufe %r ist fuer %r nicht (mehr) "
                "erlaubt — nutze Default", mode, key,
            )
            continue
        modes[key] = mode

    _cache[tenant_id] = _CacheEntry(
        modes=dict(modes), expires_at=now + _CACHE_TTL_SECONDS
    )
    return modes


async def mode_for_automation(
    tenant_id: uuid.UUID,
    automation_key: str,
) -> str:
    """Stufe einer einzelnen Automatisierung.

    Unbekannter Key -> ``manuell`` (fail-closed): lieber handelt Q nicht,
    als dass ein Tippfehler im Code zu ungefragtem Handeln fuehrt.
    """
    if automation_key not in AUTOMATIONS:
        logger.warning(
            "mode_for_automation: unbekannter Key %r", automation_key,
        )
        return MODE_MANUELL
    modes = await automation_modes_for_tenant(tenant_id)
    return modes.get(automation_key, MODE_MANUELL)


def mode_for_tool(modes: dict[str, str], tool_name: str) -> str:
    """Stufe fuer ein command_center-Write-Tool, aus einem bereits
    geladenen Modus-Dict.

    Tools ohne Registry-Eintrag laufen weiter wie bisher, also
    ``assistiert`` (Bestaetigung einholen). Ein neu hinzugefuegtes
    Write-Tool wird dadurch nie versehentlich vollautomatisch.
    """
    auto = automation_for_tool(tool_name)
    if auto is None:
        return MODE_ASSISTIERT
    return modes.get(auto.key, auto.default_mode)


async def is_automation_enabled(
    tenant_id: uuid.UUID,
    automation_key: str,
) -> bool:
    """Darf Q hier ueberhaupt selbst handeln? (alles ausser ``manuell``)

    Fuer Hintergrund-Automatisierungen, die nur zwei Zustaende kennen.
    """
    return await mode_for_automation(tenant_id, automation_key) != MODE_MANUELL


async def is_automation_automatic(
    tenant_id: uuid.UUID,
    automation_key: str,
) -> bool:
    """Darf Q ohne Rueckfrage handeln?"""
    return await mode_for_automation(
        tenant_id, automation_key
    ) == MODE_AUTOMATISCH


# =====================================================================
# Schreiben
# =====================================================================


async def set_automation_mode(
    tenant_id: uuid.UUID,
    automation_key: str,
    mode: str,
) -> bool:
    """Setzt die Stufe. Gibt False zurueck, wenn Key oder Stufe ungueltig ist.

    Upsert von Hand statt ON CONFLICT: die Tabelle ist winzig (max. eine
    Handvoll Zeilen pro Tenant) und wird nur beim Klick in den Einstellungen
    geschrieben — der Roundtrip mehr faellt nicht ins Gewicht.
    """
    if not is_valid_mode(automation_key, mode):
        return False

    from core.models import AutomationSetting

    async with AsyncSessionLocal() as session:
        row = (await session.execute(
            select(AutomationSetting)
            .where(AutomationSetting.tenant_id == tenant_id)
            .where(AutomationSetting.automation_key == automation_key)
        )).scalar_one_or_none()
        if row is None:
            session.add(AutomationSetting(
                tenant_id=tenant_id,
                automation_key=automation_key,
                mode=mode,
            ))
        else:
            row.mode = mode
        await session.commit()

    invalidate_automation_cache(tenant_id)
    return True
