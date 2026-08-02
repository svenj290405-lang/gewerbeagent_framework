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


async def einstellungen(tid: uuid.UUID) -> dict:
    """Zahlungsziel + Nachfass-Frist des Betriebs (mit Defaults).

    Failsafe: faellt auf die Defaults zurueck, wenn keine ToolConfig da ist
    oder unsinnige Werte drinstehen — eine kaputte Zahl darf die Uebersicht
    nicht kippen.
    """
    from sqlalchemy import select

    from core.database.connection import get_session
    from core.models import ToolConfig

    zahlungsziel = ZAHLUNGSZIEL_DEFAULT_TAGE
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
            zahlungsziel = int(cfg.get("zahlungsziel_tage") or zahlungsziel)
            nachfass = int(cfg.get("nachfass_tage") or nachfass)
    except (TypeError, ValueError):
        zahlungsziel, nachfass = ZAHLUNGSZIEL_DEFAULT_TAGE, NACHFASS_DEFAULT_TAGE
    except Exception:  # pragma: no cover - DB-Ausfall
        logger.warning("buchhaltung: ToolConfig nicht lesbar, nutze Defaults", exc_info=True)
    if not 1 <= zahlungsziel <= 180:
        zahlungsziel = ZAHLUNGSZIEL_DEFAULT_TAGE
    if not 1 <= nachfass <= 180:
        nachfass = NACHFASS_DEFAULT_TAGE
    return {"zahlungsziel_tage": zahlungsziel, "nachfass_tage": nachfass}


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
        "nachfass_tage": nachfass_tage,
        "kennzahlen": kennzahlen,
        "offene_posten": posten,
        "nachfassen": nachfassen,
    }


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
