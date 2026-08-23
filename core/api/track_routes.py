"""Zaehl-Endpunkt der Marketing-Website — oeffentlich, ohne Login.

Gegenstueck zu `website/z.js`. Nimmt genau zwei Ereignisarten entgegen:
ein Seitenaufruf und ein Klick auf einen Kontakt-Link. Mehr wird nicht
erhoben — jedes zusaetzliche Feld macht Besucher wiedererkennbarer, ohne
die eine Frage besser zu beantworten: kommen Leute, und melden sie sich?

Grundsaetze:

* **Antwort immer 204.** Auch bei Muell, Limit oder Datenbankfehler. Eine
  Zaehlung darf niemals eine Seite verlangsamen oder einen Fehler zeigen.
* **Keine IP, kein User-Agent im Speicher.** Beides geht nur in den
  Tages-Hash ein (siehe core/models/website_visit.py).
* **Same-Origin.** Die Seite laeuft unter www., das Framework unter der
  Hauptdomain; die CSP erlaubt aber nur `connect-src 'self'`. Deshalb
  reicht Caddy `/z/*` im www-Block an das Framework durch, statt hier
  CORS zu oeffnen.

Ehrliche Grenze: ein oeffentlicher Zaehl-Endpunkt ist nie faelschungs-
sicher. Deshalb ist die Leitkennzahl im Admin die Zahl der BESUCHER
(verschiedene Tages-Hashes), nicht die der Aufrufe — wer ueber eine
Leitung flutet, faellt zu einem Besucher zusammen.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
from threading import Lock
from urllib.parse import urlparse

from fastapi import APIRouter, Request, Response

from core.models.website_visit import (
    ALLE_ARTEN, heute_lokal, hole_salt, besucher_hash, record_website_visit,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/z", tags=["tracking"])

#: Groesser als das kann eine ehrliche Meldung nicht sein.
MAX_BODY_BYTES = 512
#: Pro Leitung und Stunde.
MAX_PRO_IP_H = 60
#: Pro Besucher und Tag — bremst auch, wenn jemand die Leitung wechselt.
MAX_PRO_BESUCHER_TAG = 30
#: Notbremse fuers Ganze, damit ein Botnetz die Zahlen nicht sprengt.
MAX_PRO_TAG = 20_000

_BOT_MUSTER = re.compile(
    r"bot|crawl|spider|slurp|headless|phantom|curl|wget|python-requests|"
    r"httpx|scrapy|monitor|preview|facebookexternalhit|lighthouse",
    re.IGNORECASE,
)

_TREFFER: dict[str, list[dt.datetime]] = {}
_TREFFER_GUARD = Lock()
_TAGESZAEHLER: dict[dt.date, int] = {}


def _ohne_port(wert: str) -> str:
    """Schneidet einen angehaengten Port ab.

    Caddy schickte lange `{remote}` statt `{remote_host}`, also
    "1.2.3.4:52344" — mit wechselndem Port bei JEDEM Aufruf. Damit war
    jede Zaehlung pro IP wirkungslos: jeder Aufruf sah aus wie ein neuer
    Absender. Der Caddyfile ist gefixt, aber die App verlaesst sich nicht
    darauf.
    """
    wert = (wert or "").strip()
    if wert.startswith("["):                      # IPv6 in Klammern
        return wert.split("]")[0].lstrip("[")[:64]
    if wert.count(":") == 1:                      # IPv4:Port
        return wert.split(":")[0][:64]
    return wert[:64]


def _client_ip(request: Request) -> str:
    """Echte Besucher-IP hinter Caddy (wird nur gehasht, nie gespeichert)."""
    xri = request.headers.get("x-real-ip")
    if xri:
        return _ohne_port(xri.split(",")[0])
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return _ohne_port(xff.split(",")[0])
    return _ohne_port(request.client.host if request.client else "unbekannt")


def _limit_frei(schluessel: str, *, max_pro_stunde: int) -> bool:
    """Gleitendes Stundenfenster, Muster aus anfrage_routes.py."""
    jetzt = dt.datetime.now(dt.timezone.utc)
    grenze = jetzt - dt.timedelta(hours=1)
    with _TREFFER_GUARD:
        treffer = [t for t in _TREFFER.get(schluessel, []) if t >= grenze]
        if len(treffer) >= max_pro_stunde:
            _TREFFER[schluessel] = treffer
            return False
        treffer.append(jetzt)
        _TREFFER[schluessel] = treffer
        if len(_TREFFER) > 5000:
            _TREFFER.clear()
    return True


def _tagesdeckel_frei(tag: dt.date) -> bool:
    with _TREFFER_GUARD:
        anzahl = _TAGESZAEHLER.get(tag, 0)
        if anzahl >= MAX_PRO_TAG:
            return False
        _TAGESZAEHLER.clear()          # nur der laufende Tag zaehlt
        _TAGESZAEHLER[tag] = anzahl + 1
    return True


def _pfad_saeubern(roh: str) -> str | None:
    pfad = (roh or "").strip()
    if not pfad.startswith("/") or len(pfad) > 200:
        return None
    # Query-String weg: dort koennen fremde Parameter mit Personenbezug
    # stehen, und fuer die Auswertung zaehlt die Seite, nicht der Aufruf.
    return pfad.split("?")[0].split("#")[0][:120]


def _ref_host(roh: str) -> str | None:
    """Nur der Host des Verweises — nie die volle Adresse."""
    roh = (roh or "").strip()
    if not roh:
        return None
    try:
        host = (urlparse(roh).hostname or "").lower()
    except ValueError:
        return None
    if not host or host.endswith("gewerbeagent.de"):
        return None                    # eigener Klickpfad, kein Verweis
    return host[:120]


@router.post("/e")
async def zaehle_ereignis(request: Request) -> Response:
    """Nimmt ein Ereignis entgegen. Antwortet IMMER mit 204."""
    leer = Response(status_code=204)
    try:
        roh = await request.body()
        if len(roh) > MAX_BODY_BYTES:
            return leer
        daten = json.loads(roh or b"{}")
        if not isinstance(daten, dict):
            return leer

        art = str(daten.get("k") or "").strip()
        if art not in ALLE_ARTEN:
            return leer
        pfad = _pfad_saeubern(str(daten.get("p") or ""))
        if pfad is None:
            return leer

        user_agent = request.headers.get("user-agent") or ""
        if not user_agent:
            return leer                # ohne Browserkennung kein Besucher
        ist_bot = bool(_BOT_MUSTER.search(user_agent))

        ip = _client_ip(request)
        if not _limit_frei(f"ip:{ip}", max_pro_stunde=MAX_PRO_IP_H):
            return leer

        tag = heute_lokal()
        if not _tagesdeckel_frei(tag):
            return leer

        salt = await hole_salt(tag)
        hash_ = besucher_hash(salt, ip, user_agent)
        if not _limit_frei(
            f"besucher:{hash_}", max_pro_stunde=MAX_PRO_BESUCHER_TAG,
        ):
            return leer

        await record_website_visit(
            tag=tag, hash_=hash_, pfad=pfad,
            ref_host=_ref_host(str(daten.get("r") or "")),
            art=art, bot=ist_bot,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("Zaehl-Endpunkt: Ereignis verworfen (%s)", exc)
    return leer
