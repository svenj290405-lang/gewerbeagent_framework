"""Google Maps Platform — Geocoding + Distance Matrix fuer Smart-Termin-Routing.

Bevorzugter Geo-Provider (vs OpenRouteService): nutzt das schon
existierende GCP-Projekt von Sven (selbes wo Vertex/Gemini laeuft) und
braucht nur einen API-Key — kein zusaetzliches Konto, keine 2k-pro-Tag-
Quota wie ORS.

Setup einmalig:
1. GCP Console → APIs & Services → Library → "Geocoding API" + "Distance
   Matrix API" aktivieren.
2. Credentials → "Create API Key" → Restriction setzen
   (HTTP-Referrer leer lassen, API-Restriction auf die beiden APIs).
3. Key in .env als GOOGLE_MAPS_API_KEY eintragen.

Endpoints:
- GET https://maps.googleapis.com/maps/api/geocode/json
  → Adresse → lat/lon (+ formatierte Adresse fuer den Cache)
- GET https://maps.googleapis.com/maps/api/distancematrix/json
  → origins[] x destinations[] → Reisezeiten (Auto, mit Traffic)

Cache: nutzen wir den existierenden geocode_cache wie ORS — der
provider-spezifische Cache-Key kollidiert nicht, weil wir den selben
normalize_address verwenden (gleiches Schema).
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import NamedTuple

import httpx
from sqlalchemy import select, update

from config.settings import settings
from core.database import AsyncSessionLocal
from core.models.geocode_cache import GeocodeCache

logger = logging.getLogger(__name__)

GMAPS_BASE = "https://maps.googleapis.com/maps/api"
GMAPS_TIMEOUT_SECONDS = 8.0
GMAPS_GEOCODE_REGION = "de"
GMAPS_GEOCODE_LANGUAGE = "de"

PROVIDER_NAME = "google_maps"


class GeoPoint(NamedTuple):
    lat: float
    lon: float


def _has_api_key() -> bool:
    return bool((settings.google_maps_api_key or "").strip())


def is_configured() -> bool:
    """True wenn der Maps-API-Key gesetzt ist."""
    return _has_api_key()


# ---------------------------------------------------------------------------
# Geocoding
# ---------------------------------------------------------------------------


async def geocode_address(
    address: str,
    *,
    use_cache: bool = True,
) -> GeoPoint | None:
    """Adresse → (lat, lon). Cache-First.

    Returns None wenn:
    - kein API-Key
    - Maps liefert ZERO_RESULTS
    - HTTP-Fehler
    """
    if not address or not address.strip():
        return None
    # Wir nutzen die selbe Normalisierung wie ORS damit Cache-Hits
    # provider-uebergreifend funktionieren (gleiche Schluessel).
    from core.integrations.openrouteservice import (
        normalize_address, _address_key,
    )
    normalized = normalize_address(address)
    if not normalized:
        return None
    key = _address_key(normalized)

    # 1. Cache lookup
    if use_cache:
        async with AsyncSessionLocal() as session:
            row = (await session.execute(
                select(GeocodeCache.lat, GeocodeCache.lon)
                .where(GeocodeCache.address_key == key)
            )).first()
            if row is not None:
                await session.execute(
                    update(GeocodeCache)
                    .where(GeocodeCache.address_key == key)
                    .values(hit_count=GeocodeCache.hit_count + 1)
                )
                await session.commit()
                return GeoPoint(float(row[0]), float(row[1]))

    if not _has_api_key():
        logger.warning(
            "google_maps.geocode_address: kein API-Key gesetzt — "
            "GOOGLE_MAPS_API_KEY in .env nachpflegen."
        )
        return None

    params = {
        "address": address,
        "key": settings.google_maps_api_key,
        "region": GMAPS_GEOCODE_REGION,
        "language": GMAPS_GEOCODE_LANGUAGE,
    }
    try:
        async with httpx.AsyncClient(timeout=GMAPS_TIMEOUT_SECONDS) as client:
            r = await client.get(f"{GMAPS_BASE}/geocode/json", params=params)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as exc:
        logger.warning(
            f"GMaps-Geocode HTTP {exc.response.status_code}: "
            f"{exc.response.text[:200]}"
        )
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"GMaps-Geocode crash: {exc}")
        return None

    status = data.get("status")
    if status != "OK":
        # ZERO_RESULTS, INVALID_REQUEST, OVER_QUERY_LIMIT, REQUEST_DENIED
        # alle landen hier — Caller faellt auf ORS oder None zurueck.
        if status == "OVER_QUERY_LIMIT":
            logger.warning("GMaps-Geocode: Quota erschoepft")
        elif status == "REQUEST_DENIED":
            logger.error(
                f"GMaps-Geocode REQUEST_DENIED — API-Key oder API-Restriction "
                f"falsch? Body: {data.get('error_message', '')[:200]}"
            )
        else:
            logger.info(f"GMaps-Geocode: status={status} fuer {address!r}")
        return None

    results = data.get("results") or []
    if not results:
        return None
    loc = (results[0].get("geometry") or {}).get("location") or {}
    lat = loc.get("lat")
    lon = loc.get("lng")
    if lat is None or lon is None:
        return None
    formatted = results[0].get("formatted_address")

    # In Cache
    try:
        async with AsyncSessionLocal() as session:
            entry = GeocodeCache(
                address_key=key,
                address_normalized=normalized[:500],
                lat=Decimal(str(round(float(lat), 6))),
                lon=Decimal(str(round(float(lon), 6))),
                formatted=formatted[:500] if formatted else None,
            )
            session.add(entry)
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"Geocode-Cache-Insert race: {exc}")

    return GeoPoint(float(lat), float(lon))


# ---------------------------------------------------------------------------
# Travel-Time (Distance Matrix API)
# ---------------------------------------------------------------------------


async def travel_time_minutes(a: GeoPoint, b: GeoPoint) -> int | None:
    """Reisezeit in Minuten a → b (Auto, ohne Verkehr-Estimate fuer
    Reproduzierbarkeit). Wir koennten 'duration_in_traffic' nutzen,
    aber das ist aufwaendiger (departure_time pflicht) und macht
    Tests instabil."""
    if not _has_api_key():
        return None
    params = {
        "origins": f"{a.lat},{a.lon}",
        "destinations": f"{b.lat},{b.lon}",
        "mode": "driving",
        "key": settings.google_maps_api_key,
        "language": GMAPS_GEOCODE_LANGUAGE,
    }
    try:
        async with httpx.AsyncClient(timeout=GMAPS_TIMEOUT_SECONDS) as client:
            r = await client.get(
                f"{GMAPS_BASE}/distancematrix/json", params=params,
            )
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as exc:
        logger.warning(
            f"GMaps-Matrix HTTP {exc.response.status_code}: "
            f"{exc.response.text[:200]}"
        )
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"GMaps-Matrix crash: {exc}")
        return None

    if data.get("status") != "OK":
        logger.info(f"GMaps-Matrix status={data.get('status')}")
        return None
    rows = data.get("rows") or []
    if not rows or not rows[0].get("elements"):
        return None
    el = rows[0]["elements"][0]
    if el.get("status") != "OK":
        return None
    dur = (el.get("duration") or {}).get("value")
    if dur is None:
        return None
    return int(round(dur / 60.0))


async def travel_time_matrix(
    points: list[GeoPoint],
) -> list[list[int | None]] | None:
    """N×N-Matrix von Reisezeiten in Minuten. None bei Fehler.

    Maps-Distance-Matrix-API hat Limit 100 origin×destination pro Call.
    Bei N>10 splitten wir nicht — Caller braucht das nicht (Skill-Router
    vorfiltert auf max 3 Kandidaten).
    """
    if not _has_api_key() or len(points) < 2:
        return None
    locs = "|".join(f"{p.lat},{p.lon}" for p in points)
    params = {
        "origins": locs,
        "destinations": locs,
        "mode": "driving",
        "key": settings.google_maps_api_key,
        "language": GMAPS_GEOCODE_LANGUAGE,
    }
    try:
        async with httpx.AsyncClient(timeout=GMAPS_TIMEOUT_SECONDS) as client:
            r = await client.get(
                f"{GMAPS_BASE}/distancematrix/json", params=params,
            )
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as exc:
        logger.warning(
            f"GMaps-Matrix HTTP {exc.response.status_code}: "
            f"{exc.response.text[:200]}"
        )
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"GMaps-Matrix crash: {exc}")
        return None

    if data.get("status") != "OK":
        return None
    out: list[list[int | None]] = []
    for row in data.get("rows") or []:
        out_row: list[int | None] = []
        for el in row.get("elements") or []:
            if el.get("status") != "OK":
                out_row.append(None)
                continue
            dur = (el.get("duration") or {}).get("value")
            out_row.append(
                int(round(dur / 60.0)) if dur is not None else None
            )
        out.append(out_row)
    return out
