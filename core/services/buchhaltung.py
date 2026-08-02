"""Buchhaltungs-Auswertung: offene Posten, Kennzahlen, Nachfass-Angebote.

Die EINE Stelle, die aus Rechnungen/Angeboten die Geld-Sicht rechnet. Genutzt
von der PWA (``GET /app/api/buchhaltung``) und von Q (Tool ``offene_posten``) —
die Zahlen duerfen zwischen den Oberflaechen nicht driften.

Alles hier ist reine Auswertung vorhandener Felder: ``Rechnung.bezahlt_am``
(setzt der Zahlungs-Monitor aus Lexware), ``Rechnung.mail_sent_at`` und
``Angebot.status``. Keine Migration noetig — das Zahlungsziel liegt in der
``config`` der ``lexware``-ToolConfig, die es ohnehin gibt.

Alle Funktionen sind tenant-gescoped (erster Parameter ``tid``) und geben
jsonable Werte zurueck (Betraege als float, Zeiten als ISO-String).
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid

logger = logging.getLogger(__name__)

# Nach wie vielen Tagen ohne Zahlungseingang eine versendete Rechnung als
# ueberfaellig gilt. Pro Betrieb ueberschreibbar via ToolConfig(lexware).config
# {"zahlungsziel_tage": 21} — bewusst ohne eigene Spalte/Migration.
ZAHLUNGSZIEL_DEFAULT_TAGE = 14

# Nach wie vielen Tagen ein versendetes Angebot ohne Rueckmeldung zum
# Nachfassen vorgeschlagen wird.
NACHFASS_DEFAULT_TAGE = 7

# Rechnungs-Status, die "Geld steht noch aus" bedeuten. ``bezahlt``,
# ``cancelled`` und ``error`` sind bewusst draussen, die Vor-Stufen
# (extracting/previewing/creating) ebenfalls — die sind noch kein Beleg.
OFFENE_RECHNUNG_STATUS = ("drafted", "mail_queued", "mail_sent")


def _to_float(v) -> float:
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return 0.0


def _tage_her(zeitpunkt: dt.datetime | None, jetzt: dt.datetime) -> int:
    if not zeitpunkt:
        return 0
    if zeitpunkt.tzinfo is None:
        zeitpunkt = zeitpunkt.replace(tzinfo=dt.timezone.utc)
    return max(0, (jetzt - zeitpunkt).days)


# Zahlungsziel aus Lexware, gecacht: tenant_id -> (wert, skonto, ablauf_ts).
# Das aendert sich hoechstens, wenn der Betrieb seine Zahlungsbedingung
# umstellt — eine Stunde alt zu sein ist voellig unkritisch, und der
# Buchhaltungs-Screen soll nicht bei jedem Aufruf gegen Lexware laufen.
_ZIEL_CACHE: dict[uuid.UUID, tuple[int | None, dict | None, float]] = {}
_ZIEL_CACHE_TTL = 3600.0


def invalidate_zahlungsziel_cache(tid: uuid.UUID | None = None) -> None:
    """Nach dem Verbinden eines neuen Lexware-Keys aufrufen."""
    if tid is None:
        _ZIEL_CACHE.clear()
    else:
        _ZIEL_CACHE.pop(tid, None)


async def _lexware_zahlungsziel(tid: uuid.UUID) -> tuple[int | None, dict | None]:
    """(Zahlungsziel in Tagen, Skonto-Bedingung) aus Lexware — oder (None, None).

    Fail-soft: Lexware unerreichbar, kein Key, kaputte Antwort → None, dann
    greift die Kaskade in ``einstellungen()``. Der Geld-Bildschirm darf nie
    an einer fremden API haengen.
    """
    import time

    treffer = _ZIEL_CACHE.get(tid)
    if treffer and treffer[2] > time.monotonic():
        return treffer[0], treffer[1]

    tage: int | None = None
    skonto: dict | None = None
    try:
        from core.integrations.rechnung_payment_monitor import _build_lexware_provider
        provider = await _build_lexware_provider(tid)
        if provider is not None:
            cond = await provider.get_default_payment_term()
            if cond:
                roh = cond.get("paymentTermDuration")
                if isinstance(roh, int) and 0 <= roh <= 180:
                    tage = roh
                rabatte = cond.get("paymentDiscountConditions")
                if isinstance(rabatte, dict) and rabatte.get("discountPercentage"):
                    skonto = {
                        "prozent": rabatte.get("discountPercentage"),
                        "tage": rabatte.get("discountRange"),
                    }
    except Exception:  # noqa: BLE001 - Lexware down/langsam darf nichts kippen
        logger.info("buchhaltung: Zahlungsziel nicht aus Lexware lesbar", exc_info=True)

    _ZIEL_CACHE[tid] = (tage, skonto, time.monotonic() + _ZIEL_CACHE_TTL)
    return tage, skonto


async def einstellungen(tid: uuid.UUID) -> dict:
    """Zahlungsziel + Nachfass-Frist des Betriebs.

    Kaskade, absichtlich in dieser Reihenfolge:
      1. ToolConfig(lexware).config — was der Betrieb bei UNS eingestellt hat
      2. Lexware-Standard-Zahlungsbedingung — was tatsaechlich auf der
         Rechnung steht (der pilot-Betrieb steht z.B. auf 0 Tagen,
         "zahlbar sofort")
      3. 14 Tage als letzte Annahme

    ``quelle`` sagt, welche Stufe gewonnen hat — die Oberflaeche soll nicht
    so tun, als waere eine Annahme eine Tatsache.

    Failsafe: unsinnige Werte fallen auf den Default zurueck, eine kaputte
    Zahl darf die Uebersicht nicht kippen.
    """
    from sqlalchemy import select

    from core.database.connection import get_session
    from core.models import ToolConfig

    eigen_ziel: int | None = None
    nachfass = NACHFASS_DEFAULT_TAGE
    try:
        async with get_session() as s:
            cfg = (await s.execute(
                select(ToolConfig.config).where(
                    ToolConfig.tenant_id == tid,
                    ToolConfig.tool_name == "lexware",
                )
            )).scalar_one_or_none() or {}
        if isinstance(cfg, dict):
            if cfg.get("zahlungsziel_tage") is not None:
                eigen_ziel = int(cfg["zahlungsziel_tage"])
            nachfass = int(cfg.get("nachfass_tage") or nachfass)
    except (TypeError, ValueError):
        eigen_ziel, nachfass = None, NACHFASS_DEFAULT_TAGE
    except Exception:  # pragma: no cover - DB-Ausfall
        logger.warning("buchhaltung: ToolConfig nicht lesbar, nutze Defaults", exc_info=True)

    skonto = None
    if eigen_ziel is not None and 0 <= eigen_ziel <= 180:
        ziel, quelle = eigen_ziel, "betrieb"
    else:
        lex_ziel, skonto = await _lexware_zahlungsziel(tid)
        if lex_ziel is not None:
            ziel, quelle = lex_ziel, "lexware"
        else:
            ziel, quelle = ZAHLUNGSZIEL_DEFAULT_TAGE, "standard"

    if not 1 <= nachfass <= 180:
        nachfass = NACHFASS_DEFAULT_TAGE
    return {
        "zahlungsziel_tage": ziel,
        "zahlungsziel_quelle": quelle,
        "skonto": skonto,
        "nachfass_tage": nachfass,
    }


async def uebersicht(tid: uuid.UUID, *, limit: int = 100) -> dict:
    """Komplette Geld-Sicht eines Betriebs.

    Rueckgabe:
      ``kennzahlen``    – Summen (offen / ueberfaellig / bezahlt 30 Tage / Angebote offen)
      ``offene_posten`` – unbezahlte Rechnungen, aelteste zuerst
      ``nachfassen``    – versendete Angebote ohne Rueckmeldung
    """
    from sqlalchemy import select

    from core.database.connection import get_session
    from core.integrations.lexware import LexwareProvider
    from core.models.angebot import Angebot, ANGEBOT_STATUS_MAIL_SENT
    from core.models.rechnung import Rechnung

    einst = await einstellungen(tid)
    ziel = einst["zahlungsziel_tage"]
    nachfass_tage = einst["nachfass_tage"]
    jetzt = dt.datetime.now(dt.timezone.utc)
    seit_30t = jetzt - dt.timedelta(days=30)

    async with get_session() as s:
        offene = (await s.execute(
            select(Rechnung)
            .where(
                Rechnung.tenant_id == tid,
                Rechnung.status.in_(OFFENE_RECHNUNG_STATUS),
                Rechnung.bezahlt_am.is_(None),
            )
            .order_by(Rechnung.created_at.asc())
            .limit(limit)
        )).scalars().all()
        bezahlt = (await s.execute(
            select(Rechnung)
            .where(
                Rechnung.tenant_id == tid,
                Rechnung.bezahlt_am.is_not(None),
                Rechnung.bezahlt_am >= seit_30t,
            )
        )).scalars().all()
        angebote_offen = (await s.execute(
            select(Angebot)
            .where(
                Angebot.tenant_id == tid,
                Angebot.status == ANGEBOT_STATUS_MAIL_SENT,
            )
            .order_by(Angebot.created_at.asc())
            .limit(limit)
        )).scalars().all()

    posten: list[dict] = []
    for r in offene:
        raus = r.mail_sent_at or r.drafted_at or r.created_at
        tage = _tage_her(raus, jetzt)
        versendet = r.mail_sent_at is not None
        posten.append({
            "id": str(r.id),
            "kunde": r.kunde_name or "—",
            "nummer": r.lexware_voucher_number or "",
            "betrag_eur": _to_float(r.betrag_brutto_eur),
            "status": r.status,
            "versendet": versendet,
            "tage": tage,
            "ueberfaellig": bool(versendet and tage > ziel),
            "seit_iso": raus.isoformat() if raus else None,
            "lexware_link": (
                LexwareProvider.invoice_deeplink_view(r.lexware_invoice_id)
                if r.lexware_invoice_id else None
            ),
        })
    # Ueberfaellige nach oben, danach die aeltesten — so steht immer oben,
    # was Geld kostet.
    posten.sort(key=lambda p: (not p["ueberfaellig"], -p["tage"]))

    nachfassen: list[dict] = []
    for a in angebote_offen:
        tage = _tage_her(a.created_at, jetzt)
        if tage < nachfass_tage:
            continue
        nachfassen.append({
            "id": str(a.id),
            "kunde": a.kunde_name or "—",
            "betrag_eur": _to_float(a.gesamtbetrag_brutto_eur),
            "tage": tage,
            "lexware_link": (
                LexwareProvider.quotation_deeplink_view(a.lexware_quotation_id)
                if a.lexware_quotation_id else None
            ),
        })
    nachfassen.sort(key=lambda n: -n["tage"])

    versendet_posten = [p for p in posten if p["versendet"]]
    ueberfaellig = [p for p in posten if p["ueberfaellig"]]
    entwuerfe = [p for p in posten if not p["versendet"]]

    kennzahlen = {
        "offen_eur": round(sum(p["betrag_eur"] for p in versendet_posten), 2),
        "offen_anzahl": len(versendet_posten),
        "ueberfaellig_eur": round(sum(p["betrag_eur"] for p in ueberfaellig), 2),
        "ueberfaellig_anzahl": len(ueberfaellig),
        "entwuerfe_anzahl": len(entwuerfe),
        "bezahlt_30t_eur": round(sum(_to_float(r.betrag_brutto_eur) for r in bezahlt), 2),
        "bezahlt_30t_anzahl": len(bezahlt),
        "angebote_offen_eur": round(
            sum(_to_float(a.gesamtbetrag_brutto_eur) for a in angebote_offen), 2),
        "angebote_offen_anzahl": len(angebote_offen),
        "nachfassen_anzahl": len(nachfassen),
    }

    return {
        "zahlungsziel_tage": ziel,
        "zahlungsziel_quelle": einst.get("zahlungsziel_quelle", "standard"),
        "skonto": einst.get("skonto"),
        "nachfass_tage": nachfass_tage,
        "kennzahlen": kennzahlen,
        "offene_posten": posten,
        "nachfassen": nachfassen,
    }


async def ausgaben(tid: uuid.UUID, *, limit: int = 25) -> dict:
    """Was der Betrieb ausgegeben hat — aus Lexware, nicht aus unserer DB.

    Wir schieben Belege bisher nur nach Lexware hinein und haben sie nie
    zurueckgelesen; deshalb konnte der Buchhaltungs-Bereich nur "was kommt
    rein" beantworten. Hier kommt die zweite Haelfte: Eingangsrechnungen
    (``purchaseinvoice``) offen + bezahlt, plus die Summe der letzten
    30 Tage.

    Zwei Lexware-Aufrufe (offen/bezahlt) — deshalb laeuft das als eigener
    Endpunkt und nicht im Haupt-Screen mit. ``ok:false`` statt Ausnahme,
    wenn Lexware nicht mitspielt: eine fehlende Ausgabenliste darf den
    Bereich nicht kippen.
    """
    jetzt = dt.datetime.now(dt.timezone.utc)
    seit_30t = jetzt - dt.timedelta(days=30)

    try:
        from core.integrations.rechnung_payment_monitor import _build_lexware_provider
        provider = await _build_lexware_provider(tid)
    except Exception:  # noqa: BLE001
        provider = None
    if provider is None:
        return {"ok": False, "error": "Buchhaltung ist nicht verbunden."}

    posten: list[dict] = []
    try:
        for status in ("open", "paid"):
            seite = await provider.get_voucherlist(
                "purchaseinvoice", status, page=0, size=limit)
            for e in seite.get("content") or []:
                datum = _parse_iso(e.get("voucherDate"))
                posten.append({
                    "id": e.get("id"),
                    "lieferant": e.get("contactName") or "—",
                    "nummer": e.get("voucherNumber") or "",
                    "betrag_eur": _to_float(e.get("totalAmount")),
                    "offen": status == "open",
                    "datum_iso": datum.isoformat() if datum else None,
                    "tage": _tage_her(datum, jetzt),
                    "lexware_link": (
                        _voucher_link(e.get("id")) if e.get("id") else None
                    ),
                })
    except Exception:  # noqa: BLE001 - Lexware down/Rate-Limit
        logger.info("buchhaltung: Ausgaben nicht abrufbar", exc_info=True)
        return {"ok": False, "error": "Ausgaben konnten nicht geladen werden."}

    posten.sort(key=lambda p: p["tage"])
    letzte_30t = [p for p in posten if p["tage"] <= 30]
    offen = [p for p in posten if p["offen"]]
    return {
        "ok": True,
        "posten": posten[:limit],
        "kennzahlen": {
            "ausgaben_30t_eur": round(sum(p["betrag_eur"] for p in letzte_30t), 2),
            "ausgaben_30t_anzahl": len(letzte_30t),
            "offen_eur": round(sum(p["betrag_eur"] for p in offen), 2),
            "offen_anzahl": len(offen),
        },
    }


def _voucher_link(voucher_id) -> str:
    """Deeplink auf einen Beleg in der Lexware-App."""
    from core.integrations.lexware import LexwareProvider
    return LexwareProvider.voucher_deeplink(voucher_id)


def _parse_iso(wert: str | None) -> dt.datetime | None:
    if not wert:
        return None
    try:
        return dt.datetime.fromisoformat(wert.replace("Z", "+00:00"))
    except ValueError:
        return None


def als_text(daten: dict, *, max_zeilen: int = 8) -> str:
    """Die Uebersicht als kurzer Fliesstext — fuer Q (Sprache/Chat).

    Bewusst knapp: Q soll den Stand vorlesen koennen, nicht eine Tabelle.
    """
    k = daten.get("kennzahlen") or {}
    posten = daten.get("offene_posten") or []
    if not posten:
        return "Es sind aktuell keine Rechnungen offen."

    def eur(v) -> str:
        return f"{float(v or 0):,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")

    kopf = (
        f"Offen: {eur(k.get('offen_eur'))} aus {k.get('offen_anzahl', 0)} "
        f"versendeten Rechnungen."
    )
    if k.get("ueberfaellig_anzahl"):
        kopf += (
            f" Davon überfällig (über {daten.get('zahlungsziel_tage')} Tage): "
            f"{eur(k.get('ueberfaellig_eur'))} aus {k['ueberfaellig_anzahl']} Rechnungen."
        )
    if k.get("entwuerfe_anzahl"):
        kopf += f" {k['entwuerfe_anzahl']} Rechnung(en) liegen noch als Entwurf herum."

    zeilen = []
    for p in posten[:max_zeilen]:
        marker = "⚠️ " if p["ueberfaellig"] else ""
        stand = f"seit {p['tage']} Tagen" if p["versendet"] else "noch nicht versendet"
        nummer = f" ({p['nummer']})" if p["nummer"] else ""
        zeilen.append(f"{marker}{p['kunde']}{nummer}: {eur(p['betrag_eur'])} — {stand}")
    if len(posten) > max_zeilen:
        zeilen.append(f"… und {len(posten) - max_zeilen} weitere.")
    return kopf + "\n" + "\n".join(zeilen)
