"""Geo-Wrapper: einheitliches Interface fuer Geocoding + Travel-Time.

Strategie: Google Maps bevorzugt, OpenRouteService als Fallback.
- Wenn GOOGLE_MAPS_API_KEY gesetzt → Maps (40k/Monat Free-Credit,
  selber GCP-Account wie Vertex/Gemini).
- Sonst wenn OPENROUTESERVICE_API_KEY gesetzt → ORS (2k/Tag Free,
  EU-hosted).
- Sonst None (Smart-Routing pausiert, Termine werden trotzdem
  gebucht — nur ohne Fahrtzeit-Optimierung).

Cache ist provider-uebergreifend (gleiche normalize_address-Logik),
sodass ein Wechsel ORS → Maps die Cache-Hits behaelt.

Alle Caller (handler.py, kalender/handler.py, employee_router.py)
importieren ab Phase 6 aus diesem Modul, nicht mehr direkt aus
openrouteservice oder google_maps.
"""
from __future__ import annotations

import logging
from typing import NamedTuple

from config.settings import settings

logger = logging.getLogger(__name__)


class GeoPoint(NamedTuple):
    """Einheitliches GeoPoint-Tupel — kompatibel mit beiden Providern."""
    lat: float
    lon: float


def _maps_active() -> bool:
    return bool((settings.google_maps_api_key or "").strip())


def _ors_active() -> bool:
    return bool((settings.openrouteservice_api_key or "").strip())


def is_configured() -> bool:
    """True wenn IRGENDEIN Geo-Provider Key gesetzt ist."""
    return _maps_active() or _ors_active()


def active_provider() -> str:
    """'google_maps' | 'openrouteservice' | 'none' — fuer /status + Logs."""
    if _maps_active():
        return "google_maps"
    if _ors_active():
        return "openrouteservice"
    return "none"


async def geocode_address(address: str, *, use_cache: bool = True):
    """Adresse → GeoPoint. None bei kein-Provider, Fehler oder kein Treffer."""
    if _maps_active():
        from core.integrations import google_maps as gm
        result = await gm.geocode_address(address, use_cache=use_cache)
        if result is not None:
            return GeoPoint(result.lat, result.lon)
        # Bei Maps-Fehler oder ZERO_RESULTS NICHT auf ORS fallen — sonst
        # wuerden wir bei jedem Maps-Quota-Stopp ploetzlich ORS-Quota
        # mitverbrauchen. Caller weiss: None = kein Geo.
        return None
    if _ors_active():
        from core.integrations import openrouteservice as ors
        result = await ors.geocode_address(address, use_cache=use_cache)
        if result is not None:
            return GeoPoint(result.lat, result.lon)
    return None


async def travel_time_minutes(a, b) -> int | None:
    """Reisezeit in Minuten a→b. a/b sind GeoPoint (.lat/.lon)."""
    if _maps_active():
        from core.integrations import google_maps as gm
        return await gm.travel_time_minutes(
            gm.GeoPoint(a.lat, a.lon), gm.GeoPoint(b.lat, b.lon),
        )
    if _ors_active():
        from core.integrations import openrouteservice as ors
        return await ors.travel_time_minutes(
            ors.GeoPoint(a.lat, a.lon), ors.GeoPoint(b.lat, b.lon),
        )
    return None


async def travel_time_matrix(points):
    """N×N-Matrix von Reisezeiten in Minuten."""
    if not points or len(points) < 2:
        return None
    if _maps_active():
        from core.integrations import google_maps as gm
        return await gm.travel_time_matrix(
            [gm.GeoPoint(p.lat, p.lon) for p in points],
        )
    if _ors_active():
        from core.integrations import openrouteservice as ors
        return await ors.travel_time_matrix(
            [ors.GeoPoint(p.lat, p.lon) for p in points],
        )
    return None
