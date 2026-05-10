"""OpenRouteService — Geocoding + Travel-Time fuer Smart-Termin-Routing.

Free-Tier: 2.000 Requests/Tag (Geocode + Matrix gemeinsam).
EU-Hosted in Heidelberg → DSGVO-konform, passt zum 'Made in Germany'-Branding.
API-Key: settings.openrouteservice_api_key (kann leer sein → Provider
liefert None, Caller faellt sauber auf bisherige Logik zurueck).

Endpoints, die wir nutzen:
- GET https://api.openrouteservice.org/geocode/search
  → Adresse → lat/lon (Pelias-basiert)
- POST https://api.openrouteservice.org/v2/matrix/driving-car
  → Liste von Lat/Lon-Paaren → NxN-Matrix mit Reisezeiten

Wir cachen Geocoding-Ergebnisse in der DB-Tabelle geocode_cache.
Travel-Time-Matrix cachen wir NICHT — die ist tageszeit-/verkehrs-
abhaengig und wir wollen aktuelle Werte. Bei Performance-Bedarf
spaeter ein 1h-In-Memory-LRU.
"""
from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from datetime import datetime, timezone
from decimal import Decimal
from typing import NamedTuple

import httpx
from sqlalchemy import select, update

from config.settings import settings
from core.database import AsyncSessionLocal
from core.models.geocode_cache import GeocodeCache

logger = logging.getLogger(__name__)

ORS_BASE = "https://api.openrouteservice.org"
ORS_TIMEOUT_SECONDS = 8.0
ORS_GEOCODE_LANG = "de"
ORS_GEOCODE_BOUNDARY_COUNTRY = "DE"

# Provider-Name fuer api_usage_log (Admin-Cost-View).
PROVIDER_NAME = "openrouteservice"


class GeoPoint(NamedTuple):
    lat: float
    lon: float


# Normalisierungsregeln: 'Hauptstr.' und 'Hauptstrasse' und 'Haupt str ' sollen
# auf den selben Cache-Key mappen.
_STR_REPLACE = [
    ("strasse", "str"), ("straße", "str"), ("str.", "str"),
    ("platz", "pl"), ("pl.", "pl"),
    ("weg", "weg"), ("allee", "allee"),
]


def normalize_address(address: str) -> str:
    """Normalisiert eine Adresse fuer den Cache-Key."""
    if not address:
        return ""
    # Unicode-Normalisierung — Umlaute → Basis-Buchstaben
    s = unicodedata.normalize("NFKD", address)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().strip()
    s = re.sub(r"[,;]", " ", s)
    s = re.sub(r"\s+", " ", s)
    for src, dst in _STR_REPLACE:
        s = s.replace(src, dst)
    return s.strip()


def _address_key(normalized: str) -> str:
    """SHA-256-Hex der normalisierten Adresse."""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _has_api_key() -> bool:
    """Schaut ob ein ORS-API-Key gesetzt ist."""
    return bool((settings.openrouteservice_api_key or "").strip())


# ---------------------------------------------------------------------------
# Geocoding
# ---------------------------------------------------------------------------

async def geocode_address(
    address: str,
    *,
    use_cache: bool = True,
) -> GeoPoint | None:
    """Adresse → (lat, lon). None bei Fehler oder fehlendem API-Key.

    Cache-First: wenn die normalisierte Adresse in geocode_cache steht,
    Read und hit_count++. Sonst ORS-Call, Schreibe-In-Cache.
    """
    if not address or not address.strip():
        return None
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
                # Hit-Count erhoehen, aber nicht warten
                await session.execute(
                    update(GeocodeCache)
                    .where(GeocodeCache.address_key == key)
                    .values(hit_count=GeocodeCache.hit_count + 1)
                )
                await session.commit()
                return GeoPoint(float(row[0]), float(row[1]))

    # 2. ORS-Call
    if not _has_api_key():
        logger.warning(
            "geocode_address: kein ORS-API-Key gesetzt, gebe None zurueck. "
            "Setze OPENROUTESERVICE_API_KEY in .env."
        )
        return None

    params = {
        "api_key": settings.openrouteservice_api_key,
        "text": address,
        "size": 1,
        "boundary.country": ORS_GEOCODE_BOUNDARY_COUNTRY,
        "lang": ORS_GEOCODE_LANG,
    }
    try:
        async with httpx.AsyncClient(timeout=ORS_TIMEOUT_SECONDS) as client:
            r = await client.get(f"{ORS_BASE}/geocode/search", params=params)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as exc:
        logger.warning(
            f"ORS-Geocode HTTP {exc.response.status_code}: "
            f"{exc.response.text[:200]}"
        )
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"ORS-Geocode crash: {exc}")
        return None

    features = data.get("features") or []
    if not features:
        logger.info(f"ORS-Geocode: kein Treffer fuer {address!r}")
        return None
    feat = features[0]
    coords = feat.get("geometry", {}).get("coordinates") or []
    if len(coords) != 2:
        return None
    # ORS liefert [lon, lat] (GeoJSON-Konvention)
    lon, lat = coords[0], coords[1]
    formatted = (feat.get("properties") or {}).get("label")

    # 3. In Cache schreiben (best-effort)
    try:
        async with AsyncSessionLocal() as session:
            entry = GeocodeCache(
                address_key=key,
                address_normalized=normalized[:500],
                lat=Decimal(str(round(lat, 6))),
                lon=Decimal(str(round(lon, 6))),
                formatted=formatted[:500] if formatted else None,
            )
            session.add(entry)
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        # Race: paralleler Insert auf den selben address_key. Nicht
        # schlimm — der parallele hat den Cache befuellt.
        logger.debug(f"Geocode-Cache-Insert race: {exc}")

    return GeoPoint(lat, lon)


# ---------------------------------------------------------------------------
# Travel-Time
# ---------------------------------------------------------------------------

async def travel_time_minutes(
    a: GeoPoint,
    b: GeoPoint,
) -> int | None:
    """Reisezeit in Minuten von a → b mit dem Auto. None bei Fehler.

    Wir nutzen die Matrix-API auch fuer Single-Pair-Calls — das ist
    derselbe Endpoint wie fuer N×M-Matrizen, hat aber pro Call nur
    1 Request gegen das Free-Tier-Budget statt 2 (geocoding + directions).
    """
    if not _has_api_key():
        return None

    payload = {
        "locations": [
            [a.lon, a.lat],   # ORS: [lon, lat]
            [b.lon, b.lat],
        ],
        "metrics": ["duration"],
        "units": "m",
    }
    headers = {
        "Authorization": settings.openrouteservice_api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=ORS_TIMEOUT_SECONDS) as client:
            r = await client.post(
                f"{ORS_BASE}/v2/matrix/driving-car",
                headers=headers,
                json=payload,
            )
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as exc:
        logger.warning(
            f"ORS-Matrix HTTP {exc.response.status_code}: "
            f"{exc.response.text[:200]}"
        )
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"ORS-Matrix crash: {exc}")
        return None

    durations = data.get("durations")
    if not durations or not durations[0] or len(durations[0]) < 2:
        return None
    seconds = durations[0][1]
    if seconds is None:
        return None
    return int(round(seconds / 60.0))


async def travel_time_matrix(
    points: list[GeoPoint],
) -> list[list[int | None]] | None:
    """N×N-Matrix von Reisezeiten in Minuten. None bei Fehler.

    Nutzlich wenn man fuer einen Tag mit 5 Terminen alle paarweisen
    Fahrzeiten in einem einzigen API-Call holen will.
    """
    if not _has_api_key() or len(points) < 2:
        return None
    payload = {
        "locations": [[p.lon, p.lat] for p in points],
        "metrics": ["duration"],
        "units": "m",
    }
    headers = {
        "Authorization": settings.openrouteservice_api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=ORS_TIMEOUT_SECONDS) as client:
            r = await client.post(
                f"{ORS_BASE}/v2/matrix/driving-car",
                headers=headers,
                json=payload,
            )
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as exc:
        logger.warning(
            f"ORS-Matrix HTTP {exc.response.status_code}: "
            f"{exc.response.text[:200]}"
        )
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"ORS-Matrix crash: {exc}")
        return None

    durations = data.get("durations") or []
    out: list[list[int | None]] = []
    for row in durations:
        out_row = []
        for sec in row:
            if sec is None:
                out_row.append(None)
            else:
                out_row.append(int(round(sec / 60.0)))
        out.append(out_row)
    return out


def is_configured() -> bool:
    """Schnelle Health-Check fuer Caller, die wissen wollen ob Smart-
    Routing ueberhaupt aktiv sein kann."""
    return _has_api_key()
