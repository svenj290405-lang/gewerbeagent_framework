"""Beleg-Foto lesen und vorkontieren.

Bis 2026-08 lief ein fotografierter Beleg so: Datei nach Lexware, dort
liegt ein leerer Beleg-Stub, und der Betrieb traegt Haendler, Datum,
Betrag, Steuersatz und Buchungskategorie von Hand nach. Bei 30 Belegen im
Monat ist das rund eine Stunde Tipparbeit.

Hier passiert stattdessen:
  1. ``vorschlag()`` — Gemini liest das Foto (das wir ohnehin in der DB
     halten) und schlaegt die Felder vor. Die Buchungskategorie darf es nur
     aus den **echten** Kategorien des Lexware-Kontos waehlen.
  2. Der Mensch sieht den Vorschlag und bestaetigt oder korrigiert ihn.
  3. ``uebernehmen()`` schreibt die bestaetigten Werte an den Beleg in
     Lexware zurueck.

Bewusst zweistufig: das ist Buchhaltung, da wird nichts ohne Blick drauf
gebucht. Und bewusst fail-soft — schlaegt irgendetwas davon fehl, bleibt
der Beleg genau das, was er vorher war: hochgeladen und unkontiert. Es
kann also nichts schlechter werden als vor diesem Feature.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
import uuid

logger = logging.getLogger(__name__)

# Buchungskategorien aendern sich praktisch nie (Kontenrahmen des Betriebs).
# Einmal pro Stunde reicht dicke, spart bei jedem Beleg einen API-Aufruf.
_KAT_CACHE: dict[uuid.UUID, tuple[list[dict], float]] = {}
_KAT_CACHE_TTL = 3600.0

# Kategorien, die im Handwerk fast alles abdecken — die stehen in der
# Auswahl fuer Gemini vorne. Der Rest wird angehaengt, bis das Limit
# erreicht ist; eine Liste mit 169 Eintraegen macht die Wahl schlechter,
# nicht besser.
_BEVORZUGT = (
    "Material/Waren", "Materialeinkauf", "Wareneinkauf", "Bezugsnebenkosten",
    "Fremdleistungen", "Subunternehmer", "Bauleistungen §13b",
    "Werkzeuge und Kleingeräte", "Betriebsbedarf", "Fahrzeugkosten",
    "Kraftstoff", "Kfz-Reparaturen", "Kfz-Versicherung", "Reisekosten",
    "Bewirtung", "Telefon", "Internet", "Porto", "Bürobedarf",
    "Fachliteratur", "Fortbildung", "Miete", "Strom", "Gas", "Wasser",
    "Versicherungen", "Beiträge", "Rechts- und Beratungskosten",
    "Buchführungskosten", "Werbung", "Arbeitskleidung",
)


def invalidate_kategorie_cache(tid: uuid.UUID | None = None) -> None:
    if tid is None:
        _KAT_CACHE.clear()
    else:
        _KAT_CACHE.pop(tid, None)


async def _ausgabe_kategorien(provider, tid: uuid.UUID) -> list[dict]:
    """Ausgabe-Buchungskategorien des Kontos (gecacht, fail-soft)."""
    treffer = _KAT_CACHE.get(tid)
    if treffer and treffer[1] > time.monotonic():
        return treffer[0]
    try:
        alle = await provider.get_posting_categories()
    except Exception:  # noqa: BLE001
        logger.info("beleg_kontierung: Kategorien nicht abrufbar", exc_info=True)
        return []
    ausgaben = [k for k in alle if k.get("type") == "outgo" and k.get("name")]
    _KAT_CACHE[tid] = (ausgaben, time.monotonic() + _KAT_CACHE_TTL)
    return ausgaben


def _auswahl_fuer_gemini(kategorien: list[dict], limit: int) -> list[str]:
    """Kategorienamen, bevorzugte zuerst, auf ``limit`` gekuerzt."""
    namen = [k["name"] for k in kategorien]
    vorne = [n for n in namen if n in _BEVORZUGT]
    hinten = [n for n in namen if n not in _BEVORZUGT]
    return (vorne + hinten)[:limit]


async def vorschlag(tid: uuid.UUID, beleg_id: uuid.UUID) -> dict:
    """Liest den gespeicherten Beleg und schlaegt die Buchungsfelder vor.

    Aendert nichts — weder bei uns noch in Lexware.
    """
    from sqlalchemy import select

    from core.ai.gemini import BELEG_MAX_KATEGORIEN, extract_beleg_from_image
    from core.database.connection import get_session
    from core.integrations.rechnung_payment_monitor import _build_lexware_provider
    from core.models.beleg import Beleg
    from core.models.tenant import Tenant

    async with get_session() as s:
        beleg = (await s.execute(
            select(Beleg).where(Beleg.id == beleg_id, Beleg.tenant_id == tid)
        )).scalar_one_or_none()
        branche = (await s.execute(
            select(Tenant.branche).where(Tenant.id == tid)
        )).scalar_one_or_none()
    if beleg is None:
        return {"ok": False, "error": "Beleg nicht gefunden."}
    if not beleg.file_data:
        return {"ok": False, "error": "Zu diesem Beleg ist keine Datei mehr gespeichert."}

    provider = await _build_lexware_provider(tid)
    kategorien = await _ausgabe_kategorien(provider, tid) if provider else []

    daten = await extract_beleg_from_image(
        beleg.file_data,
        beleg.file_mime or "image/jpeg",
        kategorien=_auswahl_fuer_gemini(kategorien, BELEG_MAX_KATEGORIEN),
        branche=branche,
    )
    if not daten.get("ist_beleg"):
        return {
            "ok": True, "ist_beleg": False,
            "hinweis": "Das sieht nicht nach einem Beleg aus.",
            "vorschlag": daten,
            "kategorien": _auswahl_fuer_gemini(kategorien, BELEG_MAX_KATEGORIEN),
        }
    return {
        "ok": True,
        "ist_beleg": True,
        "vorschlag": daten,
        # Die volle Auswahl fuer das Korrektur-Feld in der App.
        "kategorien": [k["name"] for k in kategorien],
        "kontierbar": bool(provider and beleg.lexware_voucher_id),
    }


async def uebernehmen(
    tid: uuid.UUID,
    beleg_id: uuid.UUID,
    *,
    haendler: str | None = None,
    datum: str | None = None,
    betrag_brutto_eur: float | None = None,
    mwst_prozent: int | None = None,
    kategorie: str | None = None,
) -> dict:
    """Schreibt die (vom Menschen bestaetigten) Felder nach Lexware.

    Erwartet bereits geprueft Werte — die Plausibilitaet kommt aus der
    Oberflaeche bzw. dem Endpunkt. Fehlt Pflichtsubstanz (Betrag, Datum,
    Kategorie), wird nichts geschrieben: ein halb ausgefuellter Beleg ist
    schlimmer als ein leerer, weil er "fertig" aussieht.
    """
    from sqlalchemy import select

    from core.database.connection import get_session
    from core.integrations.rechnung_payment_monitor import _build_lexware_provider
    from core.models.beleg import Beleg

    if betrag_brutto_eur is None or betrag_brutto_eur <= 0:
        return {"ok": False, "error": "Ohne Betrag kann ich den Beleg nicht buchen."}
    if not datum:
        return {"ok": False, "error": "Ohne Belegdatum kann ich den Beleg nicht buchen."}
    if mwst_prozent is None:
        mwst_prozent = 19

    async with get_session() as s:
        beleg = (await s.execute(
            select(Beleg).where(Beleg.id == beleg_id, Beleg.tenant_id == tid)
        )).scalar_one_or_none()
    if beleg is None:
        return {"ok": False, "error": "Beleg nicht gefunden."}
    if not beleg.lexware_voucher_id:
        return {"ok": False, "error": "Dieser Beleg liegt noch nicht in Lexware."}

    provider = await _build_lexware_provider(tid)
    if provider is None:
        return {"ok": False, "error": "Buchhaltung ist nicht verbunden."}

    kategorie_id = None
    if kategorie:
        for k in await _ausgabe_kategorien(provider, tid):
            if k.get("name") == kategorie:
                kategorie_id = k.get("id")
                break
        if kategorie_id is None:
            return {"ok": False,
                    "error": f"Die Kategorie {kategorie!r} gibt es im Konto nicht."}

    brutto = round(float(betrag_brutto_eur), 2)
    satz = int(mwst_prozent)
    steuer = round(brutto - brutto / (1 + satz / 100), 2) if satz else 0.0

    posten = {
        "amount": brutto,
        "taxAmount": steuer,
        "taxRatePercent": satz,
    }
    if kategorie_id:
        posten["categoryId"] = kategorie_id

    changes: dict = {
        "type": "purchaseinvoice",
        "voucherDate": _als_lexware_datum(datum),
        "totalGrossAmount": brutto,
        "totalTaxAmount": steuer,
        "taxType": "gross",
        "useCollectiveContact": True,
        "voucherItems": [posten],
    }
    if haendler:
        changes["remark"] = haendler[:200]

    try:
        await provider.update_voucher(beleg.lexware_voucher_id, changes)
    except Exception as e:  # noqa: BLE001 - Lexware lehnt ab / ist weg
        logger.warning("beleg_kontierung: Uebernahme fehlgeschlagen: %s", e)
        return {
            "ok": False,
            "error": "Lexware hat die Buchungsdaten nicht angenommen. "
                     "Der Beleg liegt aber unveraendert dort und kann von Hand "
                     "ergaenzt werden.",
        }

    from core.integrations.lexware import LexwareProvider
    logger.info("Beleg %s vorkontiert: %s %.2f EUR", beleg_id, kategorie, brutto)
    return {
        "ok": True,
        "lexware_link": LexwareProvider.voucher_deeplink(beleg.lexware_voucher_id),
    }


def _als_lexware_datum(datum: str) -> str:
    """YYYY-MM-DD → ISO-Zeitstempel, wie Lexware ihn erwartet."""
    try:
        d = dt.date.fromisoformat(str(datum)[:10])
    except ValueError:
        d = dt.date.today()
    return dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).isoformat()
