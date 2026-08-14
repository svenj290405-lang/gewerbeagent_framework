"""Fachliche Screen-Endpunkte der PWA (Welle 1e).

Tagesfunktionen: Dashboard, Termine (read-only Liste), Anrufe/Aufnahmen,
Rueckrufe. Mutierende Aktionen (Rueckruf abhaken, Termin stornieren) rufen
exakt die Logik auf, die auch der Telegram-Bot nutzt — kalender-Plugin via
``get_plugin_for_tenant`` + ``cancel_appointment`` + Storno-Mail.

HARTE Tenant-Isolation: jede Query/Aktion scoped auf
``current_tenant_id(request)``; ein Mitarbeiter kann nichts aus einem
fremden Betrieb sehen oder aendern.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, select

from core.database.connection import get_session
from core.models.angebot import (
    Angebot,
    ANGEBOT_STATUS_ABGEBROCHEN,
    ANGEBOT_STATUS_ACCEPTED,
    ANGEBOT_STATUS_MAIL_SENT,
    ANGEBOT_STATUS_RECHNUNG_ERSTELLT,
    ANGEBOT_STATUS_RECHNUNG_GESENDET,
    ANGEBOT_STATUS_WORK_DONE,
    ANGEBOT_STATUS_WORK_IN_PROGRESS,
    AUFTRAG_LIFECYCLE,
    AUFTRAG_LIFECYCLE_LABELS,
)
from core.models.employee import Employee, get_employees_for_tenant
from core.models.employee_absence import (
    get_active_absences,
    get_upcoming_absences,
)
from core.models.anfrage import AnfrageToken
from core.models.email_conversation import (
    CLASSIFICATION_NICHT_RELEVANT,
    CLASSIFICATION_PRIVAT,
    EmailConversation,
    STATE_CLOSED,
)
from core.models.kundengespraech import (
    Kundengespraech,
    KUNDENGESPRAECH_STATUS_ABGELEHNT,
    KUNDENGESPRAECH_STATUS_ABGESCHLOSSEN,
    KUNDENGESPRAECH_STATUS_ANGENOMMEN,
    KUNDENGESPRAECH_STATUS_ERFASST,
    KUNDENGESPRAECH_STATUS_VERWORFEN,
)
from core.models.rechnung import Rechnung
from core.models.tenant_knowledge import KATEGORIE_LABELS, TenantKnowledge
from core.models.rueckruf import (
    RUECKRUF_STATUS_ERLEDIGT,
    RUECKRUF_STATUS_OFFEN,
    Rueckruf,
)
from core.plugin_system import get_plugin_for_tenant
from core.security.app_auth import (
    current_tenant_id,
    require_app_csrf,
    require_app_inhaber,
    require_app_user,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/app/api", tags=["app-screens"])


def _fmt_dt(d: dt.datetime | None) -> str:
    if not d:
        return ""
    return d.strftime("%d.%m. %H:%M")


def _fmt_eur(v) -> str:
    if v is None:
        return ""
    try:
        return f"{float(v):,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return ""


# Status -> nutzerfreundliches Label + Pill-Farbe (ok|warn|danger|"")
_ANGEBOT_LABELS = {
    "erstellt": ("Entwurf", "warn"),
    "in_lexware": ("In Lexware", ""),
    "mail_queued": ("Mail in Warteschlange", "warn"),
    "mail_sent": ("Versendet", "ok"),
    "mail_failed": ("Mail fehlgeschlagen", "danger"),
    "accepted": ("Angenommen", "ok"),
    "rejected": ("Abgelehnt", "danger"),
    "rechnung_erstellt": ("Rechnung erstellt", "ok"),
}
_RECHNUNG_LABELS = {
    "extracting": ("Wird erfasst", "warn"),
    "previewing": ("Vorschau", "warn"),
    "creating": ("Wird erstellt", "warn"),
    "drafted": ("Entwurf", "warn"),
    "mail_queued": ("Mail in Warteschlange", "warn"),
    "mail_sent": ("Versendet", "ok"),
    "bezahlt": ("Bezahlt", "ok"),
    "error": ("Fehler", "danger"),
    "cancelled": ("Storniert", "danger"),
}


def _label(table: dict, status: str | None) -> tuple[str, str]:
    return table.get(status or "", (status or "—", ""))


# Auftrags-Status im "Aktuelles"-Tab: Angebot-Status -> (Label, Pill).
# Deckt die Pipeline ab Versand ab (mail_sent) bis Rechnung raus.
_AKTUELLES_AUFTRAG_LABELS = {
    ANGEBOT_STATUS_MAIL_SENT: ("Angebot versendet", "ok"),
    ANGEBOT_STATUS_ACCEPTED: ("Angenommen", "ok"),
    ANGEBOT_STATUS_RECHNUNG_ERSTELLT: ("Angebot raus", ""),
    ANGEBOT_STATUS_WORK_IN_PROGRESS: ("In Arbeit", "warn"),
    ANGEBOT_STATUS_WORK_DONE: ("Fertig – Rechnung", "ok"),
    ANGEBOT_STATUS_RECHNUNG_GESENDET: ("Rechnung versendet", "ok"),
}
# Status, die als laufender Auftrag in "Aktuelles" erscheinen.
_AKTUELLES_AUFTRAG_STATES = list(_AKTUELLES_AUFTRAG_LABELS.keys())


# =====================================================================
# Datenquellen (alle tenant-gescoped)
# =====================================================================

async def _open_rueckrufe(tenant_id: uuid.UUID) -> list[dict]:
    async with get_session() as s:
        rows = (await s.execute(
            select(Rueckruf)
            .where(Rueckruf.tenant_id == tenant_id)
            .where(Rueckruf.status == RUECKRUF_STATUS_OFFEN)
            .order_by(Rueckruf.created_at.desc())
        )).scalars().all()
    return [{
        "id": str(r.id),
        "kunde": r.kunde_name,
        "telefon": r.kunde_telefon,
        "anliegen": r.anliegen or "",
    } for r in rows]


async def _recent_aufnahmen(tenant_id: uuid.UUID, limit: int = 20) -> list[dict]:
    async with get_session() as s:
        rows = (await s.execute(
            select(Kundengespraech)
            .where(Kundengespraech.tenant_id == tenant_id)
            .where(Kundengespraech.status != KUNDENGESPRAECH_STATUS_ABGELEHNT)
            .order_by(Kundengespraech.gespraech_datum.desc())
            .limit(limit)
        )).scalars().all()
    return [{
        "id": str(k.id),
        "kunde": k.kunde_name,
        "briefing": (k.briefing_kurz or "")[:160],
        "zeit": _fmt_dt(k.gespraech_datum),
        "abgeschlossen": k.status == KUNDENGESPRAECH_STATUS_ABGESCHLOSSEN,
    } for k in rows]


async def _termine(tenant_id: uuid.UUID, *, only_today: bool, limit: int = 50) -> list[dict]:
    """Anstehende Termine aus kundengespraeche.termin_datum (lokale Quelle).

    only_today=True -> nur der heutige Tag; sonst ab jetzt aufwaerts.
    """
    now = dt.datetime.now()
    async with get_session() as s:
        stmt = (
            select(Kundengespraech)
            .where(Kundengespraech.tenant_id == tenant_id)
            .where(Kundengespraech.termin_datum.is_not(None))
            .where(Kundengespraech.status != KUNDENGESPRAECH_STATUS_ABGELEHNT)
        )
        if only_today:
            start = dt.datetime(now.year, now.month, now.day)
            end = start + dt.timedelta(days=1)
            stmt = stmt.where(Kundengespraech.termin_datum >= start).where(
                Kundengespraech.termin_datum < end)
        else:
            stmt = stmt.where(Kundengespraech.termin_datum >= now)
        stmt = stmt.order_by(Kundengespraech.termin_datum.asc()).limit(limit)
        rows = (await s.execute(stmt)).scalars().all()
    return [{
        "id": str(k.id),
        "kunde": k.kunde_name,
        "ort": k.termin_ort or "",
        "zeit": _fmt_dt(k.termin_datum),
        "termin_iso": k.termin_datum.isoformat() if k.termin_datum else None,
    } for k in rows]


# =====================================================================
# READ-Endpunkte
# =====================================================================

@router.get("/dashboard")
async def api_dashboard(request: Request, _e=Depends(require_app_user)) -> JSONResponse:
    tid = current_tenant_id(request)
    return JSONResponse({
        "termine_heute": await _termine(tid, only_today=True),
        "rueckrufe": await _open_rueckrufe(tid),
        "aufnahmen": (await _recent_aufnahmen(tid, limit=5)),
    })


@router.get("/termine")
async def api_termine(request: Request, _e=Depends(require_app_user)) -> JSONResponse:
    tid = current_tenant_id(request)
    return JSONResponse({"termine": await _termine(tid, only_today=False)})


@router.get("/aufnahmen")
async def api_aufnahmen(request: Request, _e=Depends(require_app_user)) -> JSONResponse:
    tid = current_tenant_id(request)
    return JSONResponse({"aufnahmen": await _recent_aufnahmen(tid)})


@router.get("/rueckrufe")
async def api_rueckrufe(request: Request, _e=Depends(require_app_user)) -> JSONResponse:
    tid = current_tenant_id(request)
    return JSONResponse({"rueckrufe": await _open_rueckrufe(tid)})


async def _angebote_liste(tid: uuid.UUID, limit: int = 50) -> list[dict]:
    """Angebots-Zeilen fuer die App — inkl. Lexware-Deeplink."""
    from core.integrations.lexware import LexwareProvider
    async with get_session() as s:
        rows = (await s.execute(
            select(Angebot)
            .where(Angebot.tenant_id == tid)
            .order_by(Angebot.created_at.desc())
            .limit(limit)
        )).scalars().all()
    out = []
    for a in rows:
        label, pill = _label(_ANGEBOT_LABELS, a.status)
        out.append({
            "id": str(a.id),
            "kunde": a.kunde_name,
            "betrag": _fmt_eur(a.gesamtbetrag_brutto_eur),
            "status": label,
            "pill": pill,
            "zeit": _fmt_dt(a.created_at),
            "lexware_link": (
                LexwareProvider.quotation_deeplink_view(a.lexware_quotation_id)
                if a.lexware_quotation_id else None
            ),
        })
    return out


async def _rechnungen_liste(tid: uuid.UUID, limit: int = 50) -> list[dict]:
    """Rechnungs-Zeilen fuer die App — inkl. Lexware-Deeplink."""
    from core.integrations.lexware import LexwareProvider
    async with get_session() as s:
        rows = (await s.execute(
            select(Rechnung)
            .where(Rechnung.tenant_id == tid)
            .order_by(Rechnung.created_at.desc())
            .limit(limit)
        )).scalars().all()
    out = []
    for r in rows:
        label, pill = _label(_RECHNUNG_LABELS, r.status)
        out.append({
            "id": str(r.id),
            "kunde": r.kunde_name or "—",
            "betrag": _fmt_eur(r.betrag_brutto_eur),
            "nummer": r.lexware_voucher_number or "",
            "status": label,
            "pill": pill,
            "zeit": _fmt_dt(r.created_at),
            "bezahlt": r.bezahlt_am is not None,
            "lexware_link": (
                LexwareProvider.invoice_deeplink_view(r.lexware_invoice_id)
                if r.lexware_invoice_id else None
            ),
        })
    return out


@router.get("/angebote")
async def api_angebote(request: Request, _e=Depends(require_app_user)) -> JSONResponse:
    return JSONResponse({"angebote": await _angebote_liste(current_tenant_id(request))})


@router.get("/rechnungen")
async def api_rechnungen(request: Request, _e=Depends(require_app_user)) -> JSONResponse:
    return JSONResponse({"rechnungen": await _rechnungen_liste(current_tenant_id(request))})


@router.get("/buchhaltung")
async def api_buchhaltung(request: Request, _e=Depends(require_app_user)) -> JSONResponse:
    """Alles fuer den Buchhaltungs-Bereich in EINEM Aufruf.

    Fasst zusammen, was vorher auf drei Screens verstreut war (Angebote,
    Rechnungen, Belege) und ergaenzt die Geld-Sicht aus
    ``core.services.buchhaltung``: offene Posten, ueberfaellige Rechnungen,
    Angebote zum Nachfassen, Kennzahlen.

    Feature-Gate ``lexware``: ohne Buchhaltungs-Anbindung gibt es hier nichts
    zu sehen — dann kommt ``ok:false`` statt leerer Listen, damit die App
    einen ehrlichen Hinweis zeigen kann.
    """
    from core.features.check import is_feature_enabled
    from core.services import buchhaltung as buch

    tid = current_tenant_id(request)
    if not await is_feature_enabled(tid, "lexware"):
        return JSONResponse(
            {"ok": False, "error": "Die Buchhaltung ist für diesen Betrieb nicht aktiv."},
            status_code=403,
        )
    geld, angebote, rechnungen, belege = await asyncio.gather(
        buch.uebersicht(tid),
        _angebote_liste(tid),
        _rechnungen_liste(tid),
        _recent_belege(tid),
    )
    return JSONResponse({
        "ok": True,
        "kennzahlen": geld["kennzahlen"],
        "zahlungsziel_tage": geld["zahlungsziel_tage"],
        "zahlungsziel_quelle": geld.get("zahlungsziel_quelle", "standard"),
        "skonto": geld.get("skonto"),
        "offene_posten": geld["offene_posten"],
        "nachfassen": geld["nachfassen"],
        "angebote": angebote,
        "rechnungen": rechnungen,
        "belege": belege,
    })


@router.post("/erinnerung/entwurf")
async def api_erinnerung_entwurf(
    request: Request,
    _e=Depends(require_app_inhaber),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Formuliert eine Zahlungserinnerung oder ein Angebots-Nachfassen.

    Body: ``typ`` = "zahlung" | "nachfass", ``id`` = Rechnungs- bzw.
    Angebots-ID, optional ``ton`` (freundlich|bestimmt|letzte).
    Verschickt nichts — der Text geht zurueck in die App, wo er gelesen
    und geaendert wird.

    Inhaber-Gate: wer mahnt, spricht fuer den Betrieb.
    """
    from core.features.check import is_feature_enabled
    from core.services import erinnerung

    tid = current_tenant_id(request)
    if not await is_feature_enabled(tid, "lexware"):
        return JSONResponse({"ok": False, "error": "Die Buchhaltung ist nicht aktiv."},
                            status_code=403)
    body = await request.json() if (await request.body()) else {}
    typ = (body.get("typ") or "").strip()
    try:
        oid = uuid.UUID(str(body.get("id") or ""))
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "ungueltige id"}, status_code=400)

    if typ == "zahlung":
        return JSONResponse(await erinnerung.entwurf_zahlungserinnerung(
            tid, oid, ton=(body.get("ton") or "freundlich")))
    if typ == "nachfass":
        return JSONResponse(await erinnerung.entwurf_nachfassen(tid, oid))
    return JSONResponse({"ok": False, "error": "Unbekannter Typ."}, status_code=400)


@router.post("/erinnerung/senden")
async def api_erinnerung_senden(
    request: Request,
    emp: Employee = Depends(require_app_inhaber),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Verschickt den freigegebenen Erinnerungs-/Nachfass-Text per Mail."""
    from core.features.check import is_feature_enabled
    from core.services import erinnerung

    tid = current_tenant_id(request)
    if not await is_feature_enabled(tid, "lexware"):
        return JSONResponse({"ok": False, "error": "Die Buchhaltung ist nicht aktiv."},
                            status_code=403)
    body = await request.json() if (await request.body()) else {}
    return JSONResponse(await erinnerung.senden(
        tid,
        to_email=(body.get("empfaenger") or "").strip(),
        betreff=(body.get("betreff") or "").strip(),
        text=(body.get("text") or "").strip(),
        employee_id=getattr(emp, "id", None),
    ))


@router.get("/buchhaltung/ausgaben")
async def api_buchhaltung_ausgaben(
    request: Request, _e=Depends(require_app_user),
) -> JSONResponse:
    """Eingangsrechnungen aus Lexware — die Ausgabenseite.

    Bewusst ein eigener Endpunkt: das sind zwei Lexware-Aufrufe (offen +
    bezahlt) und damit deutlich langsamer als der Rest. Die App laedt den
    Abschnitt nach, der Haupt-Screen bleibt schnell und funktioniert auch,
    wenn Lexware gerade nicht mag.
    """
    from core.features.check import is_feature_enabled
    from core.services import buchhaltung as buch

    tid = current_tenant_id(request)
    if not await is_feature_enabled(tid, "lexware"):
        return JSONResponse(
            {"ok": False, "error": "Die Buchhaltung ist für diesen Betrieb nicht aktiv."},
            status_code=403,
        )
    return JSONResponse(await buch.ausgaben(tid))


# =====================================================================
# Aufträge-Lifecycle-Board (Angebot >= rechnung_erstellt = laufender Auftrag)
# =====================================================================

# Status, die das Board direkt setzen darf. Der finale Schritt
# ``rechnung_gesendet`` ist BEWUSST ausgeschlossen: er loest in der
# Telegram-Pipeline (_run_rechnung_versand_pipeline) die Lexware-
# Finalisierung + Rechnungs-Mail aus — ein Geld-Pfad, der hier nicht
# dupliziert wird. Rechnung versenden laeuft ueber den eigenen
# Rechnungs-Flow.
_AUFTRAG_SETTABLE = {
    ANGEBOT_STATUS_ACCEPTED,
    ANGEBOT_STATUS_WORK_IN_PROGRESS,
    ANGEBOT_STATUS_WORK_DONE,
    ANGEBOT_STATUS_ABGEBROCHEN,
}


@router.get("/auftraege")
async def api_auftraege(
    request: Request, _e=Depends(require_app_user),
) -> JSONResponse:
    """LAUFENDE Auftraege = Angebote im Lifecycle, an denen noch gearbeitet
    wird. Fertiggestellte fallen raus — abgerechnete (rechnung_gesendet)
    genauso wie abgebrochene: an beiden ist nichts mehr zu tun, sie stehen
    in der Auftragshistorie (``/auftraege/historie``). Sonst waechst die
    Arbeitsliste ewig. Tenant-gescoped."""
    tid = current_tenant_id(request)
    relevante = set(AUFTRAG_LIFECYCLE) - {ANGEBOT_STATUS_RECHNUNG_GESENDET}
    async with get_session() as s:
        rows = (await s.execute(
            select(Angebot)
            .where(Angebot.tenant_id == tid, Angebot.status.in_(relevante))
            .order_by(Angebot.created_at.desc())
            .limit(50)
        )).scalars().all()
    zeilen = [_auftrag_zeile(a) for a in rows]
    await _stunden_anreichern(tid, zeilen)
    return JSONResponse({"auftraege": zeilen})


async def _stunden_anreichern(tid: uuid.UUID, zeilen: list[dict]) -> None:
    """Haengt die gebuchten Stunden an fertige Listenzeilen — EINE
    Sammelabfrage fuer alle Karten statt einer pro Karte.

    Nur laufende Auftraege: an einem abgerechneten Auftrag traegt niemand
    mehr Stunden ein, und die Karte soll nicht mit Zahlen zuwachsen.

    Best-effort: die Stundenzeile ist eine Zusatzinfo auf der Karte, die
    verbindliche Ansicht ist das Auftragsdetail. Faellt die Abfrage aus,
    fehlt die Zeile — die Auftragsliste selbst muss trotzdem stehen.
    """
    from core.services.auftrag_stunden import summen_je_auftrag

    kandidaten = [z for z in zeilen if z.get("in_arbeit") or z.get("fertig")]
    if not kandidaten:
        return
    try:
        ids = [uuid.UUID(z["id"]) for z in kandidaten]
    except (ValueError, TypeError, KeyError):
        return
    try:
        summen = await summen_je_auftrag(tid, ids)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Stundensummen fuer die Auftragsliste nicht ladbar: %s", exc)
        return
    for z in kandidaten:
        daten = summen.get(z["id"])
        z["stunden_gesamt"] = daten["gesamt_text"] if daten else ""
        z["stunden_text"] = daten["text"] if daten else ""


def _auftrag_zeile(a: Angebot) -> dict:
    """Listen-Repraesentation eines Auftrags (laufend wie abgeschlossen)."""
    return {
        "id": str(a.id),
        "kunde": a.kunde_name,
        "betrag": _fmt_eur(a.gesamtbetrag_brutto_eur),
        "status": a.status,
        "status_label": AUFTRAG_LIFECYCLE_LABELS.get(a.status, a.status),
        "schritt": AUFTRAG_LIFECYCLE.index(a.status) if a.status in AUFTRAG_LIFECYCLE else None,
        "schritte_gesamt": len(AUFTRAG_LIFECYCLE),
        "abgebrochen": a.status == ANGEBOT_STATUS_ABGEBROCHEN,
        "in_arbeit": a.status == ANGEBOT_STATUS_WORK_IN_PROGRESS,
        "fertig": a.status == ANGEBOT_STATUS_WORK_DONE,
        "fortschritt": int(a.arbeit_fortschritt or 0),
        "zeit": _fmt_dt(a.created_at),
        "abgeschlossen_am": _fmt_dt(a.abgeschlossen_am) if a.abgeschlossen_am else "",
        "archiv_url": a.archiv_drive_folder_url or "",
    }


def _historie_zeile(a: Angebot) -> dict:
    """Listenzeile fuer die Auftragshistorie — Auftragszeile plus die Frage,
    die dort als einzige zaehlt: wann war der Auftrag durch?

    Abgerechnete tragen ``abgeschlossen_am``. Abgebrochene haben keinen
    eigenen Stempel — dort ist ``updated_at`` der Zeitpunkt des Abbruchs,
    denn danach wird an einem abgebrochenen Auftrag nichts mehr geaendert.
    """
    beendet = a.abgeschlossen_am or a.updated_at
    return {
        **_auftrag_zeile(a),
        "beendet_am": _fmt_dt(beendet) if beendet else "",
    }


@router.get("/auftraege/abgeschlossen")
async def api_auftraege_abgeschlossen(
    request: Request, _e=Depends(require_app_user),
) -> JSONResponse:
    """Abgeschlossene Auftraege = Rechnung ist raus (rechnung_gesendet).

    Sortiert nach Abschluss-Zeitpunkt, neueste zuerst. Altbestand ohne
    ``abgeschlossen_am`` (vor Einfuehrung des Archivs versendet) faellt auf
    ``created_at`` zurueck, damit die Liste vollstaendig bleibt."""
    tid = current_tenant_id(request)
    async with get_session() as s:
        rows = (await s.execute(
            select(Angebot)
            .where(Angebot.tenant_id == tid)
            .where(Angebot.status == ANGEBOT_STATUS_RECHNUNG_GESENDET)
            .order_by(
                Angebot.abgeschlossen_am.desc().nullslast(),
                Angebot.created_at.desc(),
            )
            .limit(100)
        )).scalars().all()
    return JSONResponse({"auftraege": [_auftrag_zeile(a) for a in rows]})


# Fertiggestellt = raus aus der Arbeitsliste. Zwei Wege dahin: abgerechnet
# oder abgebrochen. Genau diese beiden bilden die Auftragshistorie.
_AUFTRAG_HISTORIE_STATES = {
    ANGEBOT_STATUS_RECHNUNG_GESENDET,
    ANGEBOT_STATUS_ABGEBROCHEN,
}


@router.get("/auftraege/historie")
async def api_auftraege_historie(
    request: Request, _e=Depends(require_app_user),
) -> JSONResponse:
    """Auftragshistorie = ALLE fertiggestellten Auftraege, abgerechnete wie
    abgebrochene. Das vollstaendige Nachschlagewerk „was hatten wir schon" —
    die abgeschlossenen allein (``/auftraege/abgeschlossen``) beantworten
    das nicht, weil ein abgebrochener Auftrag dort nie auftaucht.

    Sortiert nach dem Zeitpunkt, an dem der Auftrag durch war, neueste
    zuerst. Altbestand ohne ``abgeschlossen_am`` faellt auf ``updated_at``
    zurueck, damit die Liste vollstaendig bleibt."""
    tid = current_tenant_id(request)
    async with get_session() as s:
        rows = (await s.execute(
            select(Angebot)
            .where(Angebot.tenant_id == tid)
            .where(Angebot.status.in_(_AUFTRAG_HISTORIE_STATES))
            .order_by(
                func.coalesce(Angebot.abgeschlossen_am, Angebot.updated_at).desc(),
                Angebot.created_at.desc(),
            )
            .limit(200)
        )).scalars().all()
    return JSONResponse({"auftraege": [_historie_zeile(a) for a in rows]})


@router.post("/auftraege/{angebot_id}/status")
async def api_auftrag_status(
    angebot_id: str,
    request: Request,
    _e=Depends(require_app_inhaber),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Setzt den Auftrags-Status (reines DB-Update, spiegelt
    _handle_auftrag_callback). Erlaubt: accepted, arbeit_laeuft,
    arbeit_fertig, abgebrochen. ``rechnung_gesendet`` ist ausgeschlossen
    (Geld-Pfad, siehe _AUFTRAG_SETTABLE). Inhaber-only, CSRF, harte
    Tenant-Isolation."""
    tid = current_tenant_id(request)
    try:
        aid = uuid.UUID(angebot_id)
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "ungueltige id"}, status_code=400)
    body = await request.json()
    new_status = (body.get("status") or "").strip()
    if new_status not in _AUFTRAG_SETTABLE:
        return JSONResponse(
            {"ok": False, "error": "Dieser Status kann hier nicht gesetzt werden."},
            status_code=400,
        )
    async with get_session() as s:
        a = (await s.execute(
            select(Angebot).where(Angebot.id == aid, Angebot.tenant_id == tid)
        )).scalar_one_or_none()
        if a is None:
            return JSONResponse({"ok": False, "error": "Auftrag nicht gefunden."}, status_code=404)
        a.status = new_status
        if new_status == ANGEBOT_STATUS_ACCEPTED and not a.accepted_at:
            a.accepted_at = dt.datetime.now(dt.timezone.utc)
        await s.commit()
    logger.info("PWA-Auftrag Status gesetzt: id=%s tenant=%s status=%s", aid, tid, new_status)
    return JSONResponse({
        "ok": True, "status": new_status,
        "status_label": AUFTRAG_LIFECYCLE_LABELS.get(new_status, new_status),
    })


# Mehr Positionen tippt niemand von Hand — die Grenze haelt einen kaputten
# oder boesartigen Client davon ab, ueber ein Formular tausende Zeilen
# anzulegen.
_AUFTRAG_MAX_POSITIONEN = 50


@router.post("/auftraege/neu")
async def api_auftrag_neu(
    request: Request,
    _e=Depends(require_app_inhaber),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Legt einen Auftrag von Hand an — fuer Arbeit, die nie durch die
    Angebots-Pipeline lief (Telefon, Baustelle, Stammkunde).

    Ohne Lexware-Angebot; die Rechnung entsteht am Ende des Flows aus den
    Positionen. Inhaber-only, CSRF, tenant-gescoped."""
    tid = current_tenant_id(request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "Ungueltige Daten."}, status_code=400)

    positionen = body.get("positionen") or []
    if not isinstance(positionen, list):
        return JSONResponse({"ok": False, "error": "Positionen fehlen."}, status_code=400)
    if len(positionen) > _AUFTRAG_MAX_POSITIONEN:
        return JSONResponse(
            {"ok": False, "error": f"Hoechstens {_AUFTRAG_MAX_POSITIONEN} Positionen."},
            status_code=400)
    if not all(isinstance(p, dict) for p in positionen):
        return JSONResponse({"ok": False, "error": "Positionen fehlerhaft."}, status_code=400)

    from core.services.document_flow import create_auftrag_manuell
    res = await create_auftrag_manuell(
        tid,
        kunde_name=body.get("kunde_name") or "",
        positionen=positionen,
        status=body.get("status"),
        kunde_strasse=body.get("kunde_strasse"),
        kunde_plz=body.get("kunde_plz"),
        kunde_ort=body.get("kunde_ort"),
        kunde_email=body.get("kunde_email"),
    )
    if not res.get("ok"):
        return JSONResponse(res, status_code=400)
    return JSONResponse(res)


# =====================================================================
# "Aktuelles"-Tab: Rückrufe + Beratungs-Leads + Auftrags-Pipeline
# =====================================================================

async def _beratung_leads(tenant_id: uuid.UUID) -> list[dict]:
    """Offene Beratungs-Leads: Kundengespräche mit Termin, noch nicht
    entschieden (status 'erfasst'), ohne verknüpftes Angebot. Der Handwerker
    nimmt sie in 'Aktuelles' an (-> Pipeline) oder lehnt ab (-> ausgeblendet).
    """
    async with get_session() as s:
        rows = (await s.execute(
            select(Kundengespraech)
            .where(Kundengespraech.tenant_id == tenant_id)
            .where(Kundengespraech.status == KUNDENGESPRAECH_STATUS_ERFASST)
            .where(Kundengespraech.termin_datum.is_not(None))
            .where(Kundengespraech.angebot_id.is_(None))
            .order_by(Kundengespraech.termin_datum.asc())
            .limit(50)
        )).scalars().all()
        return [{
            "id": str(k.id),
            "kunde": k.kunde_name,
            "briefing": (k.briefing_kurz or "")[:200],
            "termin": _fmt_dt(k.termin_datum),
            "termin_iso": k.termin_datum.isoformat() if k.termin_datum else None,
        } for k in rows]


async def _aktuelle_auftraege(tenant_id: uuid.UUID) -> list[dict]:
    """Laufende Aufträge für 'Aktuelles': Angebote ab Versand (mail_sent) bis
    Rechnung raus, plus angenommene Beratungs-Leads, für die noch kein Angebot
    existiert ('Angebot erstellen')."""
    out: list[dict] = []
    async with get_session() as s:
        angebote = (await s.execute(
            select(Angebot)
            .where(Angebot.tenant_id == tenant_id)
            .where(Angebot.status.in_(_AKTUELLES_AUFTRAG_STATES))
            .order_by(Angebot.created_at.desc())
            .limit(50)
        )).scalars().all()
        kunde_namen = {(a.kunde_name or "").strip().lower() for a in angebote}
        for a in angebote:
            label, pill = _label(_AKTUELLES_AUFTRAG_LABELS, a.status)
            out.append({
                "typ": "auftrag",
                "id": str(a.id),
                "kunde": a.kunde_name,
                "betrag": _fmt_eur(a.gesamtbetrag_brutto_eur),
                "status": a.status,
                "status_label": label,
                "pill": pill,
                "in_arbeit": a.status == ANGEBOT_STATUS_WORK_IN_PROGRESS,
                "fertig": a.status == ANGEBOT_STATUS_WORK_DONE,
                "fortschritt": int(a.arbeit_fortschritt or 0),
                "zeit": _fmt_dt(a.created_at),
            })
        # Angenommene Leads ohne (noch) erstelltes Angebot
        leads = (await s.execute(
            select(Kundengespraech)
            .where(Kundengespraech.tenant_id == tenant_id)
            .where(Kundengespraech.status == KUNDENGESPRAECH_STATUS_ANGENOMMEN)
            .order_by(Kundengespraech.termin_datum.asc().nullslast())
            .limit(50)
        )).scalars().all()
        for k in leads:
            if (k.kunde_name or "").strip().lower() in kunde_namen:
                continue  # es gibt schon ein Angebot für den Kunden
            out.append({
                "typ": "lead_angenommen",
                "id": str(k.id),
                "kunde": k.kunde_name,
                "betrag": "",
                "status": "angenommen_lead",
                "status_label": "Angenommen – Angebot erstellen",
                "pill": "warn",
                "in_arbeit": False,
                "fertig": False,
                "fortschritt": 0,
                "zeit": _fmt_dt(k.termin_datum),
            })
    await _stunden_anreichern(tenant_id, out)
    return out


@router.get("/aktuelles")
async def api_aktuelles(request: Request, _e=Depends(require_app_user)) -> JSONResponse:
    """Aggregiert alles Relevante für den Start-Screen 'Aktuelles'."""
    tid = current_tenant_id(request)
    aufnahmen = await _recent_aufnahmen(tid, limit=20)
    return JSONResponse({
        "rueckrufe": await _open_rueckrufe(tid),
        "beratung": await _beratung_leads(tid),
        "auftraege": await _aktuelle_auftraege(tid),
        "aufnahmen_count": len(aufnahmen),
    })


# ---------------------------------------------------------------------------
# Q-Tagesbriefing: einmal morgens generiert, oben auf "Aktuelles" angezeigt.
# Cache pro (Tenant, Mitarbeiter, Datum) -> Gemini laeuft nur 1x/Tag (Kosten).
# ---------------------------------------------------------------------------
_BRIEFING_CACHE: dict = {}


def _tageszeit_gruss() -> str:
    h = dt.datetime.now().hour
    if h < 11:
        return "Guten Morgen"
    if h < 18:
        return "Guten Tag"
    return "Guten Abend"


async def _build_briefing_text(ctx) -> str:
    """Sammelt die Tagesdaten und laesst Q (Gemini) ein knappes, intelligentes
    Briefing schreiben. Faellt bei KI-Fehler auf eine deterministische
    Kurzfassung zurueck (Karte bleibt also immer gefuellt)."""
    import json
    from core.ai.command_center import (
        _run_anstehende_termine, _run_offene_anfragen, _run_team_status,
    )
    from core.ai.gemini import call_gemini

    heute = dt.date.today()
    termine = await _run_anstehende_termine(ctx, {"tage": 1})
    rueckrufe = await _open_rueckrufe(ctx.tid)
    auftraege = await _aktuelle_auftraege(ctx.tid)
    beratung = await _beratung_leads(ctx.tid)
    team = await _run_team_status(ctx, {})
    anfragen = {"anzahl": 0, "anfragen": []}
    if "mail_intake" in (ctx.features or set()):
        anfragen = await _run_offene_anfragen(ctx, {})

    abwesend = [f"{t['name']} ({t['abwesend_heute']})"
                for t in team.get("team", []) if t.get("abwesend_heute")]
    daten = {
        "heutige_termine": termine.get("termine", []),
        "offene_rueckrufe": [{"kunde": r.get("kunde"), "anliegen": r.get("anliegen")}
                             for r in rueckrufe],
        "neue_anfragen": anfragen.get("anfragen", []),
        "team_abwesend_heute": abwesend,
        "laufende_auftraege": [{"kunde": a.get("kunde"), "status": a.get("status_label")}
                               for a in auftraege],
        "neue_beratungs_leads": [{"kunde": b.get("kunde")} for b in beratung],
    }
    name = (getattr(ctx.employee, "name", "") or "").split(" ")[0] or "Chef"
    betrieb = getattr(ctx.tenant, "company_name", "") or "deinem Betrieb"
    gruss = _tageszeit_gruss()
    wochentage = ["Montag", "Dienstag", "Mittwoch", "Donnerstag",
                  "Freitag", "Samstag", "Sonntag"]
    prompt = (
        f"Du bist Q, der persoenliche Assistent von {name} bei {betrieb}. "
        f"Heute ist {wochentage[heute.weekday()]}, der {heute.strftime('%d.%m.%Y')}. "
        "Schreibe ein SEHR KURZES Tagesbriefing (2-4 Saetze, Du-Form, wie ein "
        "cleverer, souveraener Kollege - nicht steif, nicht geschwaetzig). "
        f"Beginne mit '{gruss}, {name}'. Fasse nur das Wichtigste zusammen, was "
        "waehrend seiner Abwesenheit reinkam und heute ansteht: heutige Termine, "
        "offene Rueckrufe, neue Anfragen, Team-Abwesenheiten, dringende Auftraege. "
        "KEINE Aufzaehlungszeichen, KEIN Markdown, flieSSender Text, konkret mit "
        "Zahlen und Namen. Wenn kaum etwas ansteht, sag es knapp und positiv. "
        "Erfinde nichts - nutze ausschliesslich diese Daten (JSON):\n"
        + json.dumps(daten, ensure_ascii=False)
    )
    try:
        # max_output_tokens grosszuegig: Gemini 2.5 Flash verbraucht erst Tokens
        # fuers "Thinking" — bei zu wenig bleibt die eigentliche Antwort leer
        # (dann greift unten der Fallback). 2048 reicht fuer Thinking + Kurztext.
        text = (await call_gemini(
            prompt, temperature=0.6, max_output_tokens=2048,
            tenant_id=str(ctx.tid), operation_kind="briefing")).strip()
    except Exception as e:  # noqa: BLE001
        logger.warning("Briefing-Gemini fehlgeschlagen: %s", e)
        text = ""
    if not text:
        teile = []
        if termine.get("anzahl"):
            teile.append(f"{termine['anzahl']} Termin(e) heute")
        if rueckrufe:
            teile.append(f"{len(rueckrufe)} offene(r) Rueckruf(e)")
        if anfragen.get("anzahl"):
            teile.append(f"{anfragen['anzahl']} neue Anfrage(n)")
        if abwesend:
            teile.append("abwesend: " + ", ".join(abwesend))
        kern = "; ".join(teile) if teile else "nichts Dringendes - ruhiger Tag."
        text = f"{gruss}, {name}. {kern}"
    return text


@router.get("/briefing")
async def api_briefing(request: Request, _e=Depends(require_app_user)) -> JSONResponse:
    """Q-Tagesbriefing fuer 'Aktuelles'. Einmal pro Tag generiert (Cache pro
    Tenant+Mitarbeiter+Datum); ?refresh=1 erzwingt Neuberechnung."""
    ctx = await _build_command_ctx(request)
    heute = dt.date.today().isoformat()
    emp_id = str(getattr(ctx.employee, "id", "") or "")
    key = (str(ctx.tid), emp_id, heute)
    refresh = request.query_params.get("refresh") == "1"
    cached = _BRIEFING_CACHE.get(key)
    if cached and not refresh:
        return JSONResponse({"ok": True, "text": cached, "datum": heute, "cached": True})
    text = await _build_briefing_text(ctx)
    _BRIEFING_CACHE[key] = text
    # Cache schlank halten: nur die heutigen Eintraege behalten.
    for old in [k for k in _BRIEFING_CACHE if k[2] != heute]:
        _BRIEFING_CACHE.pop(old, None)
    return JSONResponse({"ok": True, "text": text, "datum": heute, "cached": False})


@router.post("/beratung/{gespraech_id}/entscheidung")
async def api_beratung_entscheidung(
    gespraech_id: str,
    request: Request,
    _e=Depends(require_app_user),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Beratungs-Lead annehmen (-> Pipeline) oder ablehnen (-> Soft-Delete,
    ausgeblendet wie gelöscht). Tenant-gescoped, CSRF."""
    tid = current_tenant_id(request)
    try:
        gid = uuid.UUID(gespraech_id)
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "ungueltige id"}, status_code=400)
    body = await request.json()
    entscheidung = (body.get("entscheidung") or "").strip()
    if entscheidung not in ("annehmen", "ablehnen"):
        return JSONResponse({"ok": False, "error": "annehmen|ablehnen erwartet"}, status_code=400)
    neuer_status = (
        KUNDENGESPRAECH_STATUS_ANGENOMMEN if entscheidung == "annehmen"
        else KUNDENGESPRAECH_STATUS_ABGELEHNT
    )
    async with get_session() as s:
        k = (await s.execute(
            select(Kundengespraech).where(
                Kundengespraech.id == gid, Kundengespraech.tenant_id == tid)
        )).scalar_one_or_none()
        if k is None:
            return JSONResponse({"ok": False, "error": "Lead nicht gefunden."}, status_code=404)
        kunde = k.kunde_name
        k.status = neuer_status
        await s.commit()
    logger.info("PWA-Beratung %s: id=%s tenant=%s", entscheidung, gid, tid)
    return JSONResponse({"ok": True, "entscheidung": entscheidung, "kunde": kunde})


@router.post("/auftraege/{angebot_id}/fortschritt")
async def api_auftrag_fortschritt(
    angebot_id: str,
    request: Request,
    _e=Depends(require_app_user),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Setzt den Arbeits-Fortschritt 0-100 % eines laufenden Auftrags. Bei
    100 % wird der Auftrag fertiggemeldet (status arbeit_fertig); der
    Rechnungs-Schritt läuft danach im Q-Flow (eigener Inhaber-Endpunkt)."""
    tid = current_tenant_id(request)
    try:
        aid = uuid.UUID(angebot_id)
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "ungueltige id"}, status_code=400)
    body = await request.json()
    try:
        pct = int(body.get("fortschritt"))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "fortschritt (0-100) erwartet"}, status_code=400)
    pct = max(0, min(100, pct))
    async with get_session() as s:
        a = (await s.execute(
            select(Angebot).where(Angebot.id == aid, Angebot.tenant_id == tid)
        )).scalar_one_or_none()
        if a is None:
            return JSONResponse({"ok": False, "error": "Auftrag nicht gefunden."}, status_code=404)
        a.arbeit_fortschritt = pct
        fertig = False
        if pct >= 100 and a.status == ANGEBOT_STATUS_WORK_IN_PROGRESS:
            a.status = ANGEBOT_STATUS_WORK_DONE
            fertig = True
        neuer_status = a.status
        await s.commit()
    return JSONResponse({
        "ok": True, "fortschritt": pct, "fertig": fertig, "status": neuer_status,
    })


# =====================================================================
# Auftragsstunden — was am Fortschrittsregler eingetragen wird
#
# Zweck ist die Nachkalkulation („was hat der Auftrag wirklich gekostet")
# und die Abrechnung, NICHT die gesetzliche Arbeitszeiterfassung und
# erst recht keine Leistungskontrolle. Die Abgrenzung steht ausfuehrlich
# in core/models/auftrag_stunden.py.
# =====================================================================

async def _auftrag_fuer_stunden(tid: uuid.UUID, aid: uuid.UUID) -> bool:
    """Gibt es den Auftrag in DIESEM Betrieb? Mehr braucht die Buchung
    nicht zu wissen."""
    async with get_session() as s:
        treffer = (await s.execute(
            select(func.count(Angebot.id))
            .where(Angebot.id == aid, Angebot.tenant_id == tid)
        )).scalar() or 0
    return bool(treffer)


@router.post("/auftraege/{angebot_id}/stunden")
async def api_auftrag_stunden_buchen(
    angebot_id: str,
    request: Request,
    emp: Employee = Depends(require_app_user),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Bucht Arbeitsstunden auf einen Auftrag — immer auf den angemeldeten
    Mitarbeiter, nie auf jemand anderen.

    Body: ``{"stunden": "6,5", "notiz": "...", "datum": "2026-07-30"}``.
    Ohne ``datum`` zaehlt heute. Antwort enthaelt die neue Uebersicht,
    damit die Karte sich ohne zweiten Roundtrip aktualisiert.
    """
    from core.services.auftrag_stunden import (
        buche_stunden, parse_stunden, stunden_uebersicht)
    from core.models.auftrag_stunden import STUNDEN_MAX, STUNDEN_MIN

    tid = current_tenant_id(request)
    try:
        aid = uuid.UUID(angebot_id)
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "ungueltige id"}, status_code=400)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "Ungueltige Daten."}, status_code=400)

    stunden = parse_stunden((body or {}).get("stunden"))
    if stunden is None:
        return JSONResponse(
            {"ok": False,
             "error": f"Stunden zwischen {STUNDEN_MIN} und {STUNDEN_MAX} eintragen "
                      "(z.B. 6,5)."},
            status_code=400)

    datum = None
    roh_datum = ((body or {}).get("datum") or "").strip()
    if roh_datum:
        try:
            datum = dt.date.fromisoformat(roh_datum[:10])
        except ValueError:
            return JSONResponse(
                {"ok": False, "error": "Datum bitte als JJJJ-MM-TT."},
                status_code=400)
        if datum > dt.date.today():
            return JSONResponse(
                {"ok": False, "error": "Stunden für die Zukunft gibt es nicht."},
                status_code=400)

    if not await _auftrag_fuer_stunden(tid, aid):
        return JSONResponse({"ok": False, "error": "Auftrag nicht gefunden."},
                            status_code=404)

    await buche_stunden(
        tid, aid, employee_id=emp.id, employee_name=getattr(emp, "name", "") or "",
        stunden=stunden, datum=datum, notiz=(body or {}).get("notiz"))
    return JSONResponse({"ok": True, "stunden": await stunden_uebersicht(tid, aid)})


@router.post("/auftraege/{angebot_id}/stunden/{eintrag_id}/loeschen")
async def api_auftrag_stunden_loeschen(
    angebot_id: str,
    eintrag_id: str,
    request: Request,
    emp: Employee = Depends(require_app_user),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Nimmt eine Stundenbuchung zurueck (Korrektur = loeschen und neu
    buchen). Eigene Buchungen darf jeder, alle nur der Inhaber."""
    from core.services.auftrag_stunden import loesche_stunden, stunden_uebersicht

    tid = current_tenant_id(request)
    try:
        aid = uuid.UUID(angebot_id)
        eid = uuid.UUID(eintrag_id)
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "ungueltige id"}, status_code=400)

    ok, fehler, gehoert_zu = await loesche_stunden(
        tid, eid, employee_id=emp.id,
        darf_alles=bool(getattr(request.state, "app_is_inhaber", False)))
    if not ok:
        return JSONResponse(
            {"ok": False, "error": fehler},
            status_code=403 if "deine" in fehler else 404)
    if gehoert_zu != aid:
        # Der Eintrag gehoert zu einem anderen Auftrag desselben Betriebs —
        # dann stimmt die aufrufende Ansicht nicht, aber geloescht ist
        # geloescht. Uebersicht zum angefragten Auftrag zurueckgeben.
        logger.info("Stunden-Loeschung ueber fremde Auftrags-URL: %s != %s",
                    gehoert_zu, aid)
    return JSONResponse({"ok": True, "stunden": await stunden_uebersicht(tid, aid)})


# =====================================================================
# Auftrags-Detailansicht + konfigurierbarer Auftragsprozess
# =====================================================================

@router.get("/auftraege/{angebot_id}/detail")
async def api_auftrag_detail(
    angebot_id: str, request: Request, _e=Depends(require_app_user),
) -> JSONResponse:
    """Alle Infos zu EINEM Auftrag: Kunde, Positionen, Betraege, Termine,
    Drive-Archiv und die volle Fortschrittszeile (Kern-Schritte +
    eigene Schritte mit Erledigt-Zustand)."""
    from core.models.angebot_position import AngebotPosition
    from core.services.auftrag_prozess import lade_auftrag_schritte
    from core.services.auftrag_stunden import stunden_uebersicht, stunden_abgleich

    tid = current_tenant_id(request)
    try:
        aid = uuid.UUID(angebot_id)
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "ungueltige id"}, status_code=400)

    async with get_session() as s:
        a = (await s.execute(
            select(Angebot).where(Angebot.id == aid, Angebot.tenant_id == tid)
        )).scalar_one_or_none()
        if a is None:
            return JSONResponse({"ok": False, "error": "Auftrag nicht gefunden."},
                                status_code=404)
        positionen = (await s.execute(
            select(AngebotPosition)
            .where(AngebotPosition.angebot_id == aid)
            .order_by(AngebotPosition.position_nr)
        )).scalars().all()
        pos_out = [{
            "name": p.name or "",
            "beschreibung": p.beschreibung or "",
            "menge": f"{float(p.menge):g} {p.einheit or ''}".strip(),
            "preis": _fmt_eur(p.preis_brutto_eur),
        } for p in positionen]
        s.expunge(a)

    adresse = " ".join(x for x in [
        a.kunde_strasse,
        " ".join(y for y in [a.kunde_plz, a.kunde_ort] if y),
    ] if x).strip()

    stunden_daten = await stunden_uebersicht(tid, aid)

    return JSONResponse({
        "ok": True,
        **_auftrag_zeile(a),
        "adresse": adresse,
        "email": a.kunde_email or "",
        "angebot_nr": a.lexware_voucher_number or "",
        "angebot_versendet": _fmt_dt(a.mail_sent_at) if a.mail_sent_at else "",
        "angenommen_am": _fmt_dt(a.accepted_at) if a.accepted_at else "",
        "positionen": pos_out,
        "schritte": await lade_auftrag_schritte(tid, a),
        "stunden": stunden_daten,
        # Abgleich gebuchte vs. angebotene Stunden — nur Hinweis, kein Eingriff
        # in die Rechnung. None, wenn das Angebot keine Stunden-Position hat.
        "stunden_abgleich": stunden_abgleich(positionen, (stunden_daten or {}).get("gesamt")),
    })


@router.post("/auftraege/{angebot_id}/schritt")
async def api_auftrag_schritt(
    angebot_id: str,
    request: Request,
    e=Depends(require_app_user),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Hakt einen EIGENEN Prozess-Schritt fuer diesen Auftrag ab (oder nimmt
    den Haken weg). Kern-Schritte sind hier nicht setzbar — die haengen am
    Auftrags-Status und laufen ueber ``/auftraege/{id}/status``."""
    from core.services.auftrag_prozess import setze_schritt_erledigt

    tid = current_tenant_id(request)
    body = await request.json()
    try:
        aid = uuid.UUID(angebot_id)
        sid = uuid.UUID(str((body or {}).get("schritt_id")))
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "ungueltige id"}, status_code=400)
    erledigt = bool((body or {}).get("erledigt"))

    ok = await setze_schritt_erledigt(
        tid, aid, sid, erledigt=erledigt, employee_id=getattr(e, "id", None))
    if not ok:
        return JSONResponse({"ok": False, "error": "Schritt nicht gefunden."},
                            status_code=404)
    return JSONResponse({"ok": True, "erledigt": erledigt})


@router.get("/auftragsprozess")
async def api_auftragsprozess(
    request: Request, _e=Depends(require_app_user),
) -> JSONResponse:
    """Die Prozess-Definition des Betriebs als geordnete Schritt-Liste
    (Kern-Schritte gesperrt, eigene Schritte frei)."""
    from core.services.auftrag_prozess import lade_prozess

    tid = current_tenant_id(request)
    return JSONResponse({"ok": True, "schritte": await lade_prozess(tid)})


@router.post("/auftragsprozess")
async def api_auftragsprozess_speichern(
    request: Request,
    _e=Depends(require_app_inhaber),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Speichert die eigenen Zwischenschritte. Inhaber-only — der Prozess
    gilt fuer den ganzen Betrieb."""
    from core.services.auftrag_prozess import ProzessFehler, speichere_prozess

    tid = current_tenant_id(request)
    body = await request.json()
    try:
        schritte = await speichere_prozess(tid, (body or {}).get("schritte") or [])
    except ProzessFehler as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    logger.info("PWA-Auftragsprozess gespeichert: tenant=%s schritte=%d",
                tid, len(schritte))
    return JSONResponse({"ok": True, "schritte": schritte})


# =====================================================================
# Rechnungs-Flow aus "Aktuelles" (100 % -> Q): Vorschau + editierbar senden
# =====================================================================

_RECHNUNG_DEFAULT_ANSCHREIBEN = (
    "Sehr geehrte Damen und Herren,\n\nvielen Dank für Ihren Auftrag und das "
    "entgegengebrachte Vertrauen. Wie vereinbart stellen wir Ihnen die "
    "erbrachten Leistungen nachstehend in Rechnung.")


@router.get("/rechnung/vorbereiten")
async def api_rechnung_vorbereiten(
    request: Request, _e=Depends(require_app_inhaber),
) -> JSONResponse:
    """Baut die Rechnungs-Vorschau eines fertigen Auftrags (Positionen +
    Betrag) und generiert ein KI-Anschreiben — beides wird in Q angezeigt und
    kann editiert werden, bevor gesendet wird. Inhaber, feature lexware.
    Erzeugt nichts in Lexware, kein Versand."""
    from core.features.check import is_feature_enabled
    tid = current_tenant_id(request)
    if not await is_feature_enabled(tid, "lexware"):
        return JSONResponse({"ok": False, "error": "Funktion nicht freigeschaltet."}, status_code=403)
    try:
        aid = uuid.UUID(request.query_params.get("angebot_id") or "")
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "ungueltige id"}, status_code=400)

    from core.models.angebot_position import AngebotPosition
    async with get_session() as s:
        a = (await s.execute(
            select(Angebot).where(Angebot.id == aid, Angebot.tenant_id == tid)
        )).scalar_one_or_none()
        if a is None:
            return JSONResponse({"ok": False, "error": "Auftrag nicht gefunden."}, status_code=404)
        positions = (await s.execute(
            select(AngebotPosition).where(AngebotPosition.angebot_id == aid)
            .order_by(AngebotPosition.position_nr)
        )).scalars().all()
        kunde = a.kunde_name
        betrag = _fmt_eur(a.gesamtbetrag_brutto_eur)
        kunde_email = a.kunde_email or ""
        pos_list = [{
            "name": p.name,
            "menge": float(p.menge) if p.menge is not None else 1.0,
            "einheit": p.einheit or "Stück",
            "preis": _fmt_eur(p.preis_brutto_eur),
            "beschreibung": p.beschreibung or "",
        } for p in positions]
        extracted = {
            "kunde_name": a.kunde_name,
            "kunde_strasse": a.kunde_strasse, "kunde_plz": a.kunde_plz,
            "kunde_ort": a.kunde_ort,
            "gesamtbetrag_brutto_eur": float(a.gesamtbetrag_brutto_eur or 0),
            "positionen": [{
                "name": p.name,
                "menge": float(p.menge) if p.menge is not None else 1.0,
                "einheit": p.einheit, "beschreibung": p.beschreibung,
                "preis_brutto_eur": float(p.preis_brutto_eur or 0),
            } for p in positions],
        }

    anschreiben = _RECHNUNG_DEFAULT_ANSCHREIBEN
    try:
        from core.ai.gemini import generate_angebot_anschreiben
        txt = await generate_angebot_anschreiben(
            extracted,
            "Schreibe ein freundliches, knappes Anschreiben für die RECHNUNG an "
            "den Kunden: Dank für den Auftrag, die Leistungen wurden wie "
            "vereinbart erbracht, höfliche Bitte um Begleichung binnen 14 Tagen. "
            "Keine Betragsangaben im Text.",
            tenant_id=tid,
        )
        if txt and txt.strip():
            anschreiben = txt.strip()
    except Exception:  # noqa: BLE001
        logger.exception("Anschreiben-Generierung fehlgeschlagen (Fallback)")

    return JSONResponse({
        "ok": True, "angebot_id": str(aid), "kunde": kunde, "betrag": betrag,
        "kunde_email": kunde_email, "positionen": pos_list, "anschreiben": anschreiben,
    })


@router.post("/rechnung/senden")
async def api_q_rechnung_senden(
    request: Request,
    _e=Depends(require_app_inhaber),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Finalisiert die Rechnung in Lexware (mit ggf. editiertem Anschreiben)
    und schickt sie als PDF an den Kunden. Inhaber, CSRF, feature lexware."""
    from core.features.check import is_feature_enabled
    tid = current_tenant_id(request)
    if not await is_feature_enabled(tid, "lexware"):
        return JSONResponse({"ok": False, "error": "Funktion nicht freigeschaltet."}, status_code=403)
    body = await request.json()
    try:
        aid = uuid.UUID(body.get("angebot_id") or "")
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "ungueltige id"}, status_code=400)
    anschreiben = (body.get("anschreiben") or "").strip() or None
    kunde_email = (body.get("kunde_email") or "").strip() or None

    from core.services.document_flow import finalize_and_send_invoice
    res = await finalize_and_send_invoice(
        tid, angebot_id=aid, anschreiben=anschreiben, kunde_email_override=kunde_email)
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


# =====================================================================
# Team (Welle 3, Teil): Read + sichere Inhaber-Aktionen
# =====================================================================

@router.get("/team")
async def api_team(request: Request, _e=Depends(require_app_user)) -> JSONResponse:
    tid = current_tenant_id(request)
    today = dt.date.today()
    employees = await get_employees_for_tenant(tid, active_only=False)
    absent = await get_active_absences(tid, today)
    upcoming = await get_upcoming_absences(tid, days_ahead=7)

    absent_map = {emp.id: ab for emp, ab in absent}
    upcoming_by_emp: dict = {}
    for emp, ab in upcoming:
        upcoming_by_emp.setdefault(emp.id, []).append({
            "typ": ab.absence_type,
            "von": ab.start_date.strftime("%d.%m."),
            "bis": ab.end_date.strftime("%d.%m.") if ab.end_date else "offen",
        })

    # Pro-Mitarbeiter-Aktivitaet der letzten 30 Tage (Logins, Diktate,
    # Assistent-Befehle) — fuer den Inhaber sichtbar, wer die App nutzt.
    from core.models.app_usage_event import (
        usage_counts_by_employee, USAGE_LOGIN, USAGE_DIKTAT,
        USAGE_ASSISTENT_BEFEHL, USAGE_ASSISTENT_AKTION,
    )
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)
    aktivitaet = await usage_counts_by_employee(tid, since=since)

    out = []
    for e in employees:
        ab = absent_map.get(e.id)
        akt = aktivitaet.get(str(e.id), {})
        out.append({
            "slug": e.slug,
            "name": e.name,
            "is_inhaber": bool(e.is_default),
            "is_active": bool(e.is_active),
            "job_title": e.job_title or "",
            "skills": list(e.skills or []),
            "kalender_verbunden": bool(e.calendar_provider),
            # "App-Zugang eingerichtet" = kann sich wirklich anmelden.
            "app_verbunden": bool(e.contact_email or e.app_password_hash),
            "abwesend_heute": ab.absence_type if ab else None,
            "kommende_abwesenheiten": upcoming_by_emp.get(e.id, []),
            "aktivitaet_30t": {
                "logins": akt.get(USAGE_LOGIN, 0),
                "diktate": akt.get(USAGE_DIKTAT, 0),
                "assistent": akt.get(USAGE_ASSISTENT_BEFEHL, 0) + akt.get(USAGE_ASSISTENT_AKTION, 0),
            },
        })
    return JSONResponse({"team": out})


async def _get_employee_by_slug(tid: uuid.UUID, slug: str) -> Employee | None:
    async with get_session() as s:
        return (await s.execute(
            select(Employee)
            .where(Employee.tenant_id == tid)
            .where(Employee.slug == slug)
        )).scalar_one_or_none()


@router.post("/team/{slug}/aktiv")
async def api_team_set_active(
    slug: str,
    request: Request,
    _e=Depends(require_app_inhaber),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Mitarbeiter aktivieren/deaktivieren (Inhaber-only). Der Inhaber-
    Account (is_default) kann nicht deaktiviert werden."""
    tid = current_tenant_id(request)
    body = await request.json()
    active = bool((body or {}).get("active"))
    async with get_session() as s:
        emp = (await s.execute(
            select(Employee)
            .where(Employee.tenant_id == tid)
            .where(Employee.slug == slug)
        )).scalar_one_or_none()
        if emp is None:
            return JSONResponse({"ok": False, "error": "nicht gefunden"}, status_code=404)
        if emp.is_default and not active:
            return JSONResponse(
                {"ok": False, "error": "Der Inhaber-Account kann nicht deaktiviert werden."},
                status_code=400,
            )
        emp.is_active = active
    return JSONResponse({"ok": True, "is_active": active})


@router.post("/team/{slug}/profil")
async def api_team_set_profile(
    slug: str,
    request: Request,
    _e=Depends(require_app_inhaber),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Job-Titel und/oder Skills setzen (Inhaber-only)."""
    tid = current_tenant_id(request)
    body = await request.json() or {}
    async with get_session() as s:
        emp = (await s.execute(
            select(Employee)
            .where(Employee.tenant_id == tid)
            .where(Employee.slug == slug)
        )).scalar_one_or_none()
        if emp is None:
            return JSONResponse({"ok": False, "error": "nicht gefunden"}, status_code=404)
        if "job_title" in body:
            jt = (body.get("job_title") or "").strip()
            emp.job_title = jt[:100] or None
        if "skills" in body:
            skills = body.get("skills") or []
            if isinstance(skills, str):
                skills = [x.strip() for x in skills.split(",") if x.strip()]
            emp.skills = [str(x)[:50] for x in skills][:20] or None
    return JSONResponse({"ok": True})


# =====================================================================
# MUTIERENDE Aktionen (CSRF-geschuetzt, tenant-gescoped)
# =====================================================================

@router.post("/rueckrufe/erledigt")
async def api_rueckruf_erledigt(
    request: Request,
    _e=Depends(require_app_user),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    tid = current_tenant_id(request)
    emp = request.state.app_employee
    body = await request.json()
    rid = (body or {}).get("id")
    try:
        rid_uuid = uuid.UUID(str(rid))
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "ungueltige id"}, status_code=400)

    async with get_session() as s:
        r = (await s.execute(
            select(Rueckruf)
            .where(Rueckruf.id == rid_uuid)
            .where(Rueckruf.tenant_id == tid)  # Tenant-Isolation
        )).scalar_one_or_none()
        if r is None:
            return JSONResponse({"ok": False, "error": "nicht gefunden"}, status_code=404)
        r.status = RUECKRUF_STATUS_ERLEDIGT
        r.erledigt_at = dt.datetime.now(dt.timezone.utc)
        r.erledigt_by_employee_id = emp.id
    return JSONResponse({"ok": True})


@router.post("/termine/storno")
async def api_termin_storno(
    request: Request,
    _e=Depends(require_app_user),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Storniert einen Termin — spiegelt den Telegram-Storno-Wizard.

    Sicher: cancelt NUR, wenn die kalender-Suche (find_events) zu diesem
    Kunden im Zeitfenster GENAU EINEN Termin liefert. Bei 0 oder mehreren
    Treffern passiert nichts (klare Rueckmeldung), damit nie der falsche
    Termin geloescht wird.
    """
    tid = current_tenant_id(request)
    tenant = request.state.app_tenant
    body = await request.json()
    kid = (body or {}).get("id")
    try:
        kid_uuid = uuid.UUID(str(kid))
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "ungueltige id"}, status_code=400)

    async with get_session() as s:
        k = (await s.execute(
            select(Kundengespraech)
            .where(Kundengespraech.id == kid_uuid)
            .where(Kundengespraech.tenant_id == tid)  # Tenant-Isolation
        )).scalar_one_or_none()
    if k is None:
        return JSONResponse({"ok": False, "error": "Termin nicht gefunden"}, status_code=404)
    if not k.termin_datum:
        return JSONResponse({"ok": False, "error": "Kein Termin-Datum hinterlegt."}, status_code=400)

    kalender = await get_plugin_for_tenant(tenant.slug, "kalender")
    if kalender is None:
        return JSONResponse({"ok": False, "error": "Kalender nicht eingerichtet."}, status_code=400)

    # Enges Zeitfenster um den Termin (±1 Tag) + Namenssuche.
    tmin = (k.termin_datum - dt.timedelta(days=1)).isoformat()
    tmax = (k.termin_datum + dt.timedelta(days=1)).isoformat()
    try:
        found = await kalender.on_webhook("find_events", {
            "kunde_name": k.kunde_name,
            "time_min": tmin,
            "time_max": tmax,
        })
    except Exception as exc:  # noqa: BLE001
        logger.exception("app storno find_events crash: %s", exc)
        return JSONResponse({"ok": False, "error": "Suche fehlgeschlagen."}, status_code=500)

    termine = found.get("termine") or []
    if len(termine) != 1:
        return JSONResponse({
            "ok": False,
            "error": (
                "Kein eindeutiger Termin gefunden "
                f"({len(termine)} Treffer). Bitte den Termin direkt im "
                "Kalender stornieren."
            ),
        }, status_code=409)

    match = termine[0]
    event_id = match.get("event_id")
    cancel_payload: dict = {"event_id": event_id}
    emp_id_str = match.get("employee_id")
    emp_uuid: uuid.UUID | None = None
    if emp_id_str:
        try:
            emp_uuid = uuid.UUID(emp_id_str)
            cancel_payload["employee_id"] = emp_uuid
        except (ValueError, TypeError):
            pass

    try:
        res = await kalender.on_webhook("cancel_appointment", cancel_payload)
    except Exception as exc:  # noqa: BLE001
        logger.exception("app storno cancel crash: %s", exc)
        return JSONResponse({"ok": False, "error": "Stornieren fehlgeschlagen."}, status_code=500)

    if not res.get("erfolg"):
        return JSONResponse(
            {"ok": False, "error": res.get("nachricht") or "Storno fehlgeschlagen."},
            status_code=400,
        )

    # Kunde best-effort per Mail benachrichtigen (gleicher Pfad wie Bot).
    mail_sent = False
    try:
        from core.integrations.mail_pipeline import send_storno_confirmation_for_event
        mail_sent = await send_storno_confirmation_for_event(
            tenant_id=tenant.id,
            company_name=tenant.company_name or "",
            event_id=event_id,
            employee_id=emp_uuid,
            cancelled_count=1,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("app storno mail crash: %s", exc)

    return JSONResponse({"ok": True, "mail_sent": mail_sent})


# =====================================================================
# Aufnahme-Detail
# =====================================================================

@router.get("/aufnahmen/{kid}")
async def api_aufnahme_detail(
    kid: str, request: Request, _e=Depends(require_app_user),
) -> JSONResponse:
    tid = current_tenant_id(request)
    try:
        kid_uuid = uuid.UUID(kid)
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "ungueltige id"}, status_code=400)
    async with get_session() as s:
        k = (await s.execute(
            select(Kundengespraech)
            .where(Kundengespraech.id == kid_uuid)
            .where(Kundengespraech.tenant_id == tid)
        )).scalar_one_or_none()
    if k is None:
        return JSONResponse({"ok": False, "error": "nicht gefunden"}, status_code=404)
    dauer = ""
    if k.audio_dauer_sekunden:
        m, sec = divmod(int(k.audio_dauer_sekunden), 60)
        dauer = f"{m}:{sec:02d} min"
    return JSONResponse({
        "id": str(k.id),
        "kunde": k.kunde_name,
        "zeit": _fmt_dt(k.gespraech_datum),
        "dauer": dauer,
        "briefing": k.briefing_kurz or "",
        "notizen": k.notizen_lang or "",
        "todos": list(k.todos or []),
        "transkript": k.raw_transcript or "",
        "termin": _fmt_dt(k.termin_datum) if k.termin_datum else "",
        "termin_ort": k.termin_ort or "",
    })


# =====================================================================
# Sprach-Diktat aus dem Browser (Telegram-/aufnahme-Ersatz)
# =====================================================================

# Maximale Audio-Groesse — spiegelt das Telegram-Limit
# (AUFNAHME_MAX_AUDIO_BYTES = 50 MB). Der Browser kodiert client-seitig zu
# WAV 16 kHz mono (~1,9 MB/min), das Gemini nativ versteht; ein Diktat
# bleibt damit problemlos unter dem Limit.
_DIKTAT_MAX_AUDIO_BYTES = 50_000_000

# Audio-MIME-Typen, die die Gemini-Analyse nativ verarbeitet. Wird ein
# anderer Typ geschickt (z.B. webm aus einem Roh-MediaRecorder), lehnen wir
# klar ab, statt Gemini einen nicht unterstuetzten Container zu fuettern.
_DIKTAT_ALLOWED_MIMES = {
    "audio/wav", "audio/x-wav", "audio/wave",
    "audio/ogg", "audio/mpeg", "audio/mp3", "audio/flac", "audio/aac",
}


def _validate_diktat_audio(audio_bytes: bytes) -> tuple[str, int] | None:
    """Prueft die Roh-Audiodaten. Returnt (fehlertext, status) oder None."""
    if not audio_bytes:
        return ("Keine Audiodaten empfangen.", 400)
    if len(audio_bytes) > _DIKTAT_MAX_AUDIO_BYTES:
        mb = len(audio_bytes) // 1024 // 1024
        maxmb = _DIKTAT_MAX_AUDIO_BYTES // 1024 // 1024
        return (
            f"Aufnahme zu lang ({mb} MB, max {maxmb} MB). "
            "Bitte in mehrere kuerzere Aufnahmen aufteilen.",
            413,
        )
    return None


def _normalize_diktat_mime(raw: str | None) -> str | None:
    """Content-Type → erlaubter Audio-MIME oder None (= nicht unterstuetzt)."""
    mime = (raw or "").split(";")[0].strip().lower()
    return mime if mime in _DIKTAT_ALLOWED_MIMES else None


def _parse_diktat_duration(raw: str | None) -> int | None:
    """Header-Wert (Sekunden) → int, mit Plausibilitaets-Cap (≤ 24 h)."""
    if not raw:
        return None
    try:
        v = int(float(raw))
    except (TypeError, ValueError):
        return None
    return v if 0 <= v <= 24 * 3600 else None


def _parse_diktat_termin(termin_str: str | None) -> dt.datetime | None:
    """ISO-/Datums-String aus der Gemini-Extraktion → aware datetime (UTC)."""
    if not termin_str:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(termin_str[:19], fmt).replace(
                tzinfo=dt.timezone.utc
            )
        except ValueError:
            continue
    logger.warning("Diktat: termin_datum nicht parsbar: %r", termin_str)
    return None


async def _save_diktat_gespraech(
    tenant_id: uuid.UUID,
    employee_id: uuid.UUID | None,
    kunde_name: str,
    dauer: int | None,
    extracted: dict,
) -> tuple[uuid.UUID, list[dict]]:
    """Speichert ein Kundengespraech aus der Diktat-Extraktion.

    Spiegelt exakt das Mapping aus dem Telegram-Flow
    (_handle_aufnahme_audio_received); zusaetzlich wird der diktierende
    Mitarbeiter als created_by/assigned vermerkt.

    Returns: (gespraech_id, kunde_frage) — kunde_frage ist die Liste
    namensgleicher Bestandskunden (Phase 6), wenn die Zuordnung offen
    blieb; leer wenn eindeutig.
    """
    g = Kundengespraech(
        tenant_id=tenant_id,
        kunde_name=kunde_name[:300],
        audio_dauer_sekunden=dauer,
        raw_transcript=extracted.get("transcript"),
        briefing_kurz=extracted.get("briefing_kurz"),
        notizen_lang=extracted.get("notizen_lang"),
        todos=extracted.get("todos") or [],
        termin_ort=extracted.get("termin_ort"),
        termin_datum=_parse_diktat_termin(extracted.get("termin_datum")),
        confidence=extracted.get("extraction_confidence"),
        status="erfasst",
        created_by_employee_id=employee_id,
        assigned_employee_id=employee_id,
    )
    async with get_session() as s:
        s.add(g)
        # Phase 6: Nur-Name-Anlage raet nie — bei namensgleichen
        # Bestandskunden bleibt kunde_id offen, die PWA fragt nach.
        from core.services.kunde_identity import (
            kunde_anzeige_merkmal, resolve_kunde_name_only)
        kandidaten = []
        try:
            g.kunde_id, kandidaten = await resolve_kunde_name_only(
                s, tenant_id, kunde_name)
        except Exception:
            logger.exception(
                "Kundenaufloesung fehlgeschlagen — kunde_id bleibt NULL")
        frage = [{"id": str(k.id), "name": k.name,
                  "merkmal": kunde_anzeige_merkmal(k)} for k in kandidaten]
        await s.commit()
        return g.id, frage


@router.post("/aufnahmen/diktat")
async def api_aufnahme_diktat(
    request: Request,
    emp: Employee = Depends(require_app_user),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Sprach-Diktat ohne vorher gewaehlten Kunden → Gemini-Analyse →
    neues Kundengespraech (die KI zieht den Namen aus dem Gesprochenen).

    Der Hauptweg der App laeuft inzwischen ueber den Gespraechs-Bereich:
    dort steht der Kunde vorher fest und das Diktat geht an
    ``/gespraeche/{id}/diktat``. Dieser Endpunkt bleibt der Einstieg fuer
    „Kunde noch unbekannt" und spiegelt weiterhin den Telegram-Flow.

    Der Browser nimmt das Gespraech per Web-Audio auf, kodiert es
    client-seitig zu WAV (16 kHz mono — von Gemini nativ unterstuetzt) und
    schickt die rohen Bytes als Request-Body. mime kommt aus Content-Type,
    die optionale Dauer (Sekunden) aus dem Header X-Audio-Duration.

    Spiegelt exakt den Telegram-/aufnahme-Flow: gleiche Gemini-Funktion,
    gleiches Datenmodell, gleiche Pflichtfeld-Pruefung (kunde_name). HARTE
    Tenant-Isolation — gespeichert wird ausschliesslich auf
    current_tenant_id(request).
    """
    from core.ai import analyse_kundengespraech_from_audio

    audio_bytes = await request.body()
    err = _validate_diktat_audio(audio_bytes)
    if err:
        return JSONResponse({"ok": False, "error": err[0]}, status_code=err[1])

    mime = _normalize_diktat_mime(request.headers.get("content-type"))
    if mime is None:
        return JSONResponse(
            {"ok": False, "error": "Audioformat wird nicht unterstuetzt."},
            status_code=415,
        )
    dauer = _parse_diktat_duration(request.headers.get("x-audio-duration"))
    tid = current_tenant_id(request)

    try:
        extracted = await analyse_kundengespraech_from_audio(
            audio_bytes, mime_type=mime, tenant_id=tid,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("PWA-Diktat: Gemini-Analyse fehlgeschlagen: %s", exc)
        return JSONResponse(
            {"ok": False, "error": "Analyse fehlgeschlagen. Bitte erneut versuchen."},
            status_code=502,
        )

    kunde_name = (extracted.get("kunde_name") or "").strip()
    if not kunde_name:
        return JSONResponse(
            {"ok": False, "error": (
                "Kein Kundenname erkannt. Bitte erneut aufnehmen und den "
                "Namen klar nennen."
            )},
            status_code=422,
        )

    g_id, kunde_frage = await _save_diktat_gespraech(
        tid, emp.id, kunde_name, dauer, extracted)
    from core.models.app_usage_event import record_app_usage, USAGE_DIKTAT
    await record_app_usage(tid, emp.id, USAGE_DIKTAT)
    logger.info(
        "PWA-Diktat gespeichert: id=%s tenant=%s mitarbeiter=%s kunde=%r todos=%d",
        g_id, tid, emp.id, kunde_name, len(extracted.get("todos") or []),
    )
    return JSONResponse({
        "ok": True,
        "id": str(g_id),
        "kunde": kunde_name,
        "briefing": (extracted.get("briefing_kurz") or "")[:300],
        "todos": list(extracted.get("todos") or []),
        "confidence": extracted.get("extraction_confidence"),
        # Phase 6: namensgleiche Bestandskunden -> die UI fragt
        # „derselbe oder ein neuer?" (leer = eindeutig zugeordnet)
        "kunde_frage": kunde_frage,
    })


# =====================================================================
# Visualisierung (Foto → KI-Rendering via Gemini)
# =====================================================================

_VIZ_MAX_BYTES = 15_000_000  # 15 MB Eingangsfoto
_VIZ_ALLOWED_MIMES = {"image/jpeg", "image/png"}
# Stil-Boilerplate analog zum Telegram-Flow (dort VIZ_PROMPT_BOILERPLATE):
# fotorealistisch, gleiche Perspektive, nur das Beschriebene aendern.
_VIZ_BOILERPLATE = (
    "Erstelle eine fotorealistische Visualisierung auf Basis dieses Fotos. "
    "Behalte Perspektive, Raum und Proportionen bei und aendere nur das "
    "Beschriebene. Kein Text und kein Wasserzeichen im Bild."
)


def _normalize_viz_mime(raw: str | None) -> str | None:
    mime = (raw or "").split(";")[0].strip().lower()
    return mime if mime in _VIZ_ALLOWED_MIMES else None


async def _feature_enabled(tenant_id: uuid.UUID, key: str) -> bool:
    from core.features.check import enabled_features_for_tenant
    return key in await enabled_features_for_tenant(tenant_id)


@router.get("/visualisierungen")
async def api_visualisierungen(
    request: Request, _e=Depends(require_app_user),
) -> JSONResponse:
    """Letzte 20 Visualisierungen (tenant-gescoped)."""
    from core.models.visualisierung import Visualisierung, VIZ_STATUS_DONE
    tid = current_tenant_id(request)
    async with get_session() as s:
        rows = (await s.execute(
            select(Visualisierung)
            .where(Visualisierung.tenant_id == tid)
            .order_by(Visualisierung.created_at.desc())
            .limit(20)
        )).scalars().all()
    return JSONResponse({"visualisierungen": [
        {
            "id": str(v.id),
            "prompt": (v.prompt or "")[:140],
            "status": v.status,
            "fertig": v.status == VIZ_STATUS_DONE and v.result_image_data is not None,
            "zeit": _fmt_dt(v.created_at),
        } for v in rows
    ]})


@router.get("/visualisierungen/{vid}/bild")
async def api_visualisierung_bild(
    vid: str, request: Request, _e=Depends(require_app_user),
):
    """Liefert das gerenderte Ergebnisbild als Bytes (tenant-gescoped)."""
    from fastapi.responses import Response
    from core.models.visualisierung import Visualisierung
    tid = current_tenant_id(request)
    try:
        vid_uuid = uuid.UUID(vid)
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "ungueltige id"}, status_code=400)
    async with get_session() as s:
        v = (await s.execute(
            select(Visualisierung)
            .where(Visualisierung.id == vid_uuid, Visualisierung.tenant_id == tid)
        )).scalar_one_or_none()
    if v is None or not v.result_image_data:
        return JSONResponse({"ok": False, "error": "nicht gefunden"}, status_code=404)
    data = bytes(v.result_image_data)
    media = "image/jpeg" if data[:2] == b"\xff\xd8" else "image/png"
    return Response(content=data, media_type=media)


@router.post("/visualisierungen")
async def api_visualisierung_erstellen(
    request: Request,
    emp: Employee = Depends(require_app_user),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Foto + Beschreibung → Gemini-Rendering → Visualisierung gespeichert.

    Body = rohe Bild-Bytes; Content-Type = MIME (jpeg/png); ?prompt= die
    Beschreibung. Spiegelt _handle_viz_description_input: gleiche
    generate_image_from_image-Fn, gleiche Boilerplate, gleiches Modell.

    Feature-gegated (visualisierung; Bildgenerierung kostet Tokens),
    require_app_user + CSRF, HARTE Tenant-Isolation.
    """
    from core.ai import generate_image_from_image
    from core.models.visualisierung import (
        Visualisierung, VIZ_STATUS_DONE, VIZ_STATUS_FAILED, VIZ_STATUS_GENERATING,
    )

    tid = current_tenant_id(request)
    if not await _feature_enabled(tid, "visualisierung"):
        return JSONResponse({"ok": False, "error": "Funktion nicht freigeschaltet."}, status_code=403)

    image_bytes = await request.body()
    if not image_bytes:
        return JSONResponse({"ok": False, "error": "Kein Foto empfangen."}, status_code=400)
    if len(image_bytes) > _VIZ_MAX_BYTES:
        mb = len(image_bytes) // 1024 // 1024
        return JSONResponse({"ok": False, "error": f"Foto zu gross ({mb} MB, max 15 MB)."}, status_code=413)
    mime = _normalize_viz_mime(request.headers.get("content-type"))
    if mime is None:
        return JSONResponse({"ok": False, "error": "Nur JPEG- oder PNG-Fotos."}, status_code=415)
    prompt = (request.query_params.get("prompt") or "").strip()
    if len(prompt) < 5:
        return JSONResponse({"ok": False, "error": "Bitte etwas mehr beschreiben (min. 5 Zeichen)."}, status_code=400)
    prompt = prompt[:500]

    # Aus einem Kundengespräch heraus gestartet? Dann findet das Ergebnis
    # dorthin zurück — gerendert wird trotzdem hier im Q-Chat.
    gespraech_id = None
    roh = (request.query_params.get("gespraech_id") or "").strip()
    if roh:
        try:
            kandidat = uuid.UUID(roh)
        except (ValueError, TypeError):
            kandidat = None
        if kandidat is not None:
            g, _ = await _gespraech_laden(tid, kandidat)
            gespraech_id = g.id if g is not None else None

    async with get_session() as s:
        viz = Visualisierung(
            tenant_id=tid, original_image_data=image_bytes,
            prompt=prompt, status=VIZ_STATUS_GENERATING,
            gespraech_id=gespraech_id,
        )
        s.add(viz)
        await s.commit()
        await s.refresh(viz)
        viz_id = viz.id

    try:
        result = await generate_image_from_image(
            image_bytes=image_bytes,
            prompt=f"{prompt}. {_VIZ_BOILERPLATE}",
            mime_type=mime,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("PWA-Visualisierung: Generierung fehlgeschlagen: %s", exc)
        result = None

    async with get_session() as s:
        viz = (await s.execute(
            select(Visualisierung).where(Visualisierung.id == viz_id)
        )).scalar_one_or_none()
        if viz:
            if result:
                viz.result_image_data = result
                viz.status = VIZ_STATUS_DONE
                viz.completed_at = dt.datetime.now(dt.timezone.utc)
            else:
                viz.status = VIZ_STATUS_FAILED
                viz.error_message = "Modell hat kein Bild zurueckgegeben (evtl. Sicherheits-Block)"
            await s.commit()

    if not result:
        return JSONResponse({"ok": False, "error": (
            "Konnte kein Bild erzeugen (evtl. Sicherheits-Block oder unklares "
            "Foto). Bitte mit anderem Foto/Beschreibung erneut versuchen."
        )}, status_code=502)

    if gespraech_id is not None:
        # Zuordnung ans Gespräch — best-effort: das Bild ist fertig und
        # bleibt es auch, wenn die Verknüpfung scheitert.
        try:
            from core.models.gespraech_datei import (
                GespraechDatei, GESPRAECH_DATEI_VISUALISIERUNG)
            async with get_session() as s:
                s.add(GespraechDatei(
                    tenant_id=tid, gespraech_id=gespraech_id,
                    typ=GESPRAECH_DATEI_VISUALISIERUNG,
                    visualisierung_id=viz_id,
                    dateiname=f"{prompt[:60]}.png", mime="image/png",
                ))
                await s.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Visualisierung nicht ans Gespräch geknüpft: %s", exc)

    logger.info("PWA-Visualisierung fertig: id=%s tenant=%s mitarbeiter=%s", viz_id, tid, emp.id)
    return JSONResponse({
        "ok": True, "id": str(viz_id),
        "bild_url": f"/app/api/visualisierungen/{viz_id}/bild",
        "gespraech_id": str(gespraech_id) if gespraech_id else "",
    })


# =====================================================================
# Kunden-Suche (über Gespräche, Angebote, Rechnungen)
# =====================================================================

@router.get("/kunden")
async def api_kunden(
    request: Request, q: str = "", _e=Depends(require_app_user),
) -> JSONResponse:
    """Kunden-Suche. Leere Query liefert alle bekannten Kunden.

    Durchsucht Gespraeche, Angebote, Rechnungen UND TenantKundeDrive
    (Kunden die nur per Archiv-Upload existieren werden sonst nicht gefunden)."""
    from core.models.tenant_kunde_drive import TenantKundeDrive
    from core.models import Kunde
    tid = current_tenant_id(request)
    query = (q or "").strip()
    like = f"%{query}%" if query else "%"
    async with get_session() as s:
        # Kundenstamm zuerst (Phase 5): Treffer per Name ODER Mail —
        # so findet eine Mail-Suche auch Gespraeche der Person, deren
        # Zeilen selbst keine Mail tragen.
        kunden_rows = (await s.execute(
            select(Kunde).where(
                Kunde.tenant_id == tid,
                Kunde.merged_into_id.is_(None),
                (Kunde.name.ilike(like) | Kunde.email.ilike(like)),
            ).order_by(Kunde.name.asc()).limit(100)
        )).scalars().all()
        kunden_ids = [k.id for k in kunden_rows]

        def _treffer(model):
            # kunde_id-Treffer ODER Namens-ilike. Der Namens-Zweig
            # traegt Zeilen ohne kunde_id durch die Uebergangsphase
            # und faellt in Phase 7 weg.
            cond = model.kunde_name.ilike(like)
            if kunden_ids:
                cond = cond | model.kunde_id.in_(kunden_ids)
            return cond

        g = (await s.execute(
            select(Kundengespraech)
            .where(Kundengespraech.tenant_id == tid)
            .where(_treffer(Kundengespraech))
            .order_by(Kundengespraech.gespraech_datum.desc()).limit(25)
        )).scalars().all()
        a = (await s.execute(
            select(Angebot).where(Angebot.tenant_id == tid)
            .where(_treffer(Angebot))
            .order_by(Angebot.created_at.desc()).limit(25)
        )).scalars().all()
        r = (await s.execute(
            select(Rechnung).where(Rechnung.tenant_id == tid)
            .where(_treffer(Rechnung))
            .order_by(Rechnung.created_at.desc()).limit(25)
        )).scalars().all()
        drv = (await s.execute(
            select(TenantKundeDrive)
            .where(TenantKundeDrive.tenant_id == tid)
            # Nur Alt-Zeilen OHNE Kundenstamm-Verweis: Zeilen mit kunde_id
            # sind schon durch ihren Kunden-Eintrag vertreten. Ohne diesen
            # Filter taucht nach einem Merge der alte Name der Quelle
            # (kunde_name der umgehaengten Drive-Zeile) als zweiter
            # Kunde in der Liste auf.
            .where(TenantKundeDrive.kunde_id.is_(None))
            .where(_treffer(TenantKundeDrive))
            .order_by(TenantKundeDrive.kunde_name.asc()).limit(50)
        )).scalars().all()
    # Anzeigeregel (Phase 6): normal nur der Name; kollidieren Namen im
    # Ergebnis, bekommt jeder Kunde sein kleinstes unterscheidendes
    # Merkmal (Mail > letzte 4 Tel-Ziffern > Erstkontakt-Datum).
    from core.services.kunde_identity import kunde_anzeige_merkmal
    name_haeufigkeit: dict[str, int] = {}
    for k in kunden_rows:
        n = (k.name or "").strip().lower()
        name_haeufigkeit[n] = name_haeufigkeit.get(n, 0) + 1
    kunden_liste = [{
        "id": str(k.id),
        "name": k.name,
        "merkmal": (kunde_anzeige_merkmal(k)
                    if name_haeufigkeit[(k.name or "").strip().lower()] > 1
                    else None),
    } for k in kunden_rows]

    return JSONResponse({
        "q": query,
        "kunden": kunden_liste,
        "gespraeche": [{"id": str(x.id), "kunde": x.kunde_name,
                        "briefing": (x.briefing_kurz or "")[:140],
                        "zeit": _fmt_dt(x.gespraech_datum)} for x in g],
        "angebote": [{"kunde": x.kunde_name, "betrag": _fmt_eur(x.gesamtbetrag_brutto_eur),
                      "zeit": _fmt_dt(x.created_at)} for x in a],
        "rechnungen": [{"kunde": x.kunde_name or "—", "betrag": _fmt_eur(x.betrag_brutto_eur),
                        "nummer": x.lexware_voucher_number or "",
                        "zeit": _fmt_dt(x.created_at)} for x in r],
        "drive_kunden": [{"name": d.kunde_name} for d in drv],
    })


@router.post("/gespraeche/{gespraech_id}/kunde")
async def api_gespraech_kunde_zuordnen(
    gespraech_id: str,
    request: Request,
    _e=Depends(require_app_user),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Phase 6: beantwortet die Kunden-Rueckfrage nach einem Diktat —
    ordnet ein nur-Name-Gespraech einem bestehenden Kunden zu
    (body: {"kunde_id": "..."}) oder legt bewusst einen neuen an
    (body: {"neu": true}). Tenant-gescoped, CSRF."""
    from core.models import Kunde
    from core.services.kunde_identity import (
        create_kunde_explizit_neu, kunde_anzeige_merkmal)
    tid = current_tenant_id(request)
    try:
        gid = uuid.UUID(gespraech_id)
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "ungueltige id"}, status_code=400)
    body = await request.json()
    kid_raw = (body.get("kunde_id") or "").strip()
    neu = bool(body.get("neu"))
    if not kid_raw and not neu:
        return JSONResponse(
            {"ok": False, "error": "kunde_id oder neu erwartet"},
            status_code=400)
    async with get_session() as s:
        g = (await s.execute(
            select(Kundengespraech).where(
                Kundengespraech.id == gid, Kundengespraech.tenant_id == tid)
        )).scalar_one_or_none()
        if g is None:
            return JSONResponse(
                {"ok": False, "error": "Gespraech nicht gefunden."},
                status_code=404)
        if g.kunde_id is not None:
            return JSONResponse(
                {"ok": False, "error": "Schon zugeordnet."}, status_code=409)
        if neu:
            kunde = await create_kunde_explizit_neu(s, tid, g.kunde_name)
        else:
            try:
                kunde = await s.get(Kunde, uuid.UUID(kid_raw))
            except (ValueError, TypeError):
                kunde = None
            if kunde is None or kunde.tenant_id != tid:
                return JSONResponse(
                    {"ok": False, "error": "Kunde nicht gefunden."},
                    status_code=404)
        g.kunde_id = kunde.id
        antwort = {"ok": True, "kunde_id": str(kunde.id),
                   "name": kunde.name,
                   "merkmal": kunde_anzeige_merkmal(kunde)}
        await s.commit()
    logger.info("PWA-Kundenzuordnung: gespraech=%s -> kunde=%s tenant=%s",
                gid, antwort["kunde_id"], tid)
    return JSONResponse(antwort)


# =====================================================================
# Kundengespräch — der Arbeitsbereich vor Ort
#
# Ein Gespräch wird zum Kunden angelegt (nicht mehr nur als Nebenprodukt
# eines Diktats) und sammelt danach alles ein: Diktat, Handnotiz, Fotos,
# Visualisierungen. Am Ende steht die Kundenmail — die Grenze, was davon
# der Kunde sehen darf, zieht core/services/kundengespraech.py.
# =====================================================================

_GESPRAECH_MAX_NOTIZ = 5000
_GESPRAECH_MAX_FOTO_BYTES = 15_000_000
_GESPRAECH_FOTO_MIMES = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
}

# Wie weit die Geplant-Liste in den Kalender schaut. Jeder Tag ist ein
# API-Call beim Provider (parallel abgesetzt) — 7 Tage sind die Woche,
# die der Handwerker ueberblickt, ohne dass der Screen haengt.
_GESPRAECH_GEPLANT_TAGE = 7
_KALENDER_ZONE = "Europe/Berlin"

# Betreff der gebuchten Termine: "[Betrieb] Anliegen - Kunde Name"
# (plugins/kalender/handler.py). Der Betriebs-Praefix ist Rauschen, der
# Name steht hinten.
_BETREFF_PRAEFIX = re.compile(r"^\s*\[[^\]]*\]\s*")


def _kunde_aus_betreff(betreff: str) -> str:
    """Rät den Kundennamen aus einem Termin-Betreff.

    Bewusst nur ein Vorschlag: die App legt damit nichts an, sondern
    füllt das Suchfeld beim „Neues Gespräch" vor — der Handwerker
    bestätigt oder korrigiert.
    """
    rest = _BETREFF_PRAEFIX.sub("", betreff or "").strip()
    for trenner in (" - ", " – ", " — "):
        if trenner in rest:
            rest = rest.rsplit(trenner, 1)[1]
            break
    return rest.strip()[:300]


def _kalender_termin_parsen(roh) -> dt.datetime | None:
    """ISO-Zeit eines Kalendertermins → ``termin_datum``.

    Wichtig: ``termin_datum`` trägt im ganzen Projekt die LOKALE
    Wanduhrzeit mit UTC-Etikett (siehe ``_parse_diktat_termin``, und
    ``_termine`` vergleicht mit dem naiven ``now()``). Angezeigt wird
    ohne Umrechnung. Ein 14-Uhr-Termin muss deshalb als 14 Uhr in die
    DB — nicht nach echtem UTC umgerechnet, sonst zeigt die App im
    Sommer 12 Uhr an.
    """
    text = (roh or "").strip() if isinstance(roh, str) else ""
    if not text:
        return None
    try:
        wert = dt.datetime.fromisoformat(text)
    except ValueError:
        return _parse_diktat_termin(text)
    if wert.tzinfo is not None:
        try:
            from zoneinfo import ZoneInfo
            wert = wert.astimezone(ZoneInfo(_KALENDER_ZONE))
        except Exception:  # noqa: BLE001
            pass
        wert = wert.replace(tzinfo=None)
    return wert.replace(tzinfo=dt.timezone.utc)


async def _gespraech_laden(tid: uuid.UUID, gid: uuid.UUID):
    """Gespräch + angehängte Dateien, tenant-gescoped."""
    from core.models.gespraech_datei import GespraechDatei
    async with get_session() as s:
        g = (await s.execute(
            select(Kundengespraech).where(
                Kundengespraech.id == gid, Kundengespraech.tenant_id == tid)
        )).scalar_one_or_none()
        if g is None:
            return None, []
        dateien = (await s.execute(
            select(GespraechDatei)
            .where(GespraechDatei.gespraech_id == gid)
            .where(GespraechDatei.tenant_id == tid)
            .order_by(GespraechDatei.created_at.asc())
        )).scalars().all()
    return g, list(dateien)


def _datei_zeile(d) -> dict:
    """Ein Bild für die Galerie. Fotos kommen über den Drive-Proxy,
    Visualisierungen direkt aus der DB."""
    from core.models.gespraech_datei import GESPRAECH_DATEI_VISUALISIERUNG
    if d.typ == GESPRAECH_DATEI_VISUALISIERUNG and d.visualisierung_id:
        url = f"/app/api/visualisierungen/{d.visualisierung_id}/bild"
    elif d.drive_file_id:
        url = f"/app/api/archiv/datei/{d.drive_file_id}"
    else:
        url = ""
    return {
        "id": str(d.id), "typ": d.typ, "url": url,
        "name": d.dateiname or ("Visualisierung"
                                if d.typ == GESPRAECH_DATEI_VISUALISIERUNG
                                else "Foto"),
        "drive_url": d.drive_url or "",
    }


async def _kunden_kontext(tid: uuid.UUID, g) -> dict:
    """Die Kundendaten, die der Handwerker im Gespräch sehen will:
    Kontakt, Adresse, Drive-Ordner und was gerade offen ist."""
    from core.models import Kunde
    from core.models.rechnung import RECHNUNG_STATUS_BEZAHLT
    kunde = None
    async with get_session() as s:
        if g.kunde_id:
            kunde = await s.get(Kunde, g.kunde_id)
            if kunde is not None and kunde.tenant_id != tid:
                kunde = None
        # Zuordnung über den Kundenstamm, wo er gesetzt ist — sonst über
        # den Namen (Zeilen aus der Übergangsphase haben keine kunde_id).
        def _gehoert_zum_kunden(modell):
            if g.kunde_id:
                return modell.kunde_id == g.kunde_id
            return modell.kunde_name == g.kunde_name

        laufend = set(AUFTRAG_LIFECYCLE) - {ANGEBOT_STATUS_RECHNUNG_GESENDET}
        auftraege = (await s.execute(
            select(func.count(Angebot.id))
            .where(Angebot.tenant_id == tid)
            .where(_gehoert_zum_kunden(Angebot))
            .where(Angebot.status.in_(laufend))
        )).scalar() or 0
        offen = (await s.execute(
            select(func.count(Rechnung.id))
            .where(Rechnung.tenant_id == tid)
            .where(_gehoert_zum_kunden(Rechnung))
            .where(Rechnung.status != RECHNUNG_STATUS_BEZAHLT)
        )).scalar() or 0
        frueher = (await s.execute(
            select(func.count(Kundengespraech.id))
            .where(Kundengespraech.tenant_id == tid)
            .where(Kundengespraech.kunde_name == g.kunde_name)
            .where(Kundengespraech.id != g.id)
        )).scalar() or 0

    drive_url = ""
    try:
        from core.integrations.google_drive import get_kunde_folder_link
        drive_url = await get_kunde_folder_link(tid, g.kunde_name) or ""
    except Exception:  # noqa: BLE001
        # Ohne Drive-Verbindung bleibt der Link leer — das Gespräch
        # funktioniert trotzdem.
        drive_url = ""

    return {
        "kunde_id": str(g.kunde_id) if g.kunde_id else "",
        "name": g.kunde_name,
        "email": (kunde.email if kunde else "") or "",
        "telefon": (kunde.telefon if kunde else "") or "",
        "adresse": (kunde.adresse if kunde else "") or "",
        "drive_url": drive_url,
        "auftraege_laufend": int(auftraege),
        "rechnungen_offen": int(offen),
        "gespraeche_frueher": int(frueher),
    }


@router.get("/gespraeche")
async def api_gespraeche(
    request: Request, _e=Depends(require_app_user),
) -> JSONResponse:
    """Liste der Kundengespräche (dieselben Daten wie /aufnahmen)."""
    tid = current_tenant_id(request)
    return JSONResponse({"gespraeche": await _recent_aufnahmen(tid)})


@router.post("/gespraeche")
async def api_gespraech_neu(
    request: Request,
    emp: Employee = Depends(require_app_user),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Legt ein leeres Gespräch zu einem Kunden an — der Einstieg vor Ort.

    Body: {kunde_id} für einen Bestandskunden ODER {kunde_name} für einen
    neuen. Diktat, Notiz und Fotos kommen danach dazu.

    Optional aus einem geplanten Kalendertermin heraus:
    {kalender_event_id, termin_iso, termin_ort} — dann hängt das Gespräch
    am Termin und der Termin verschwindet aus der Geplant-Liste."""
    from core.models import Kunde
    tid = current_tenant_id(request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "Ungueltige Daten."}, status_code=400)

    kid_raw = (body.get("kunde_id") or "").strip()
    name = (body.get("kunde_name") or "").strip()[:300]
    event_id = (body.get("kalender_event_id") or "").strip()[:500]
    termin_ort = (body.get("termin_ort") or "").strip()[:300]
    termin = _kalender_termin_parsen(body.get("termin_iso"))
    kunde = None
    async with get_session() as s:
        if kid_raw:
            try:
                kunde = await s.get(Kunde, uuid.UUID(kid_raw))
            except (ValueError, TypeError):
                kunde = None
            if kunde is None or kunde.tenant_id != tid:
                return JSONResponse(
                    {"ok": False, "error": "Kunde nicht gefunden."}, status_code=404)
            name = kunde.name
        if len(name) < 2:
            return JSONResponse(
                {"ok": False, "error": "Bitte einen Kunden wählen oder eingeben."},
                status_code=400)

        g = Kundengespraech(
            tenant_id=tid,
            kunde_name=name,
            kunde_id=kunde.id if kunde else None,
            gespraech_datum=dt.datetime.now(dt.timezone.utc),
            status=KUNDENGESPRAECH_STATUS_ERFASST,
            created_by_employee_id=emp.id,
            assigned_employee_id=emp.id,
            kalender_event_id=event_id or None,
            termin_datum=termin,
            termin_ort=termin_ort or None,
        )
        s.add(g)
        await s.flush()
        gid = g.id
        await s.commit()
    logger.info("PWA-Gespräch angelegt: id=%s tenant=%s kunde=%r", gid, tid, name)
    return JSONResponse({"ok": True, "id": str(gid), "kunde": name})


async def _geplante_kalendertermine(
    tid: uuid.UUID, employee_id: uuid.UUID | None, tage: int,
) -> list[dict]:
    """Anstehende Termine aus dem echten Kalender des Mitarbeiters.

    Provider-agnostisch über den Kalender-Adapter (Google ODER Outlook).
    Failsafe: ohne Kalender-Verbindung kommt eine leere Liste zurück —
    die Gesprächsliste funktioniert auch ohne.
    """
    try:
        from plugins.kalender.adapters import get_calendar_adapter
        adapter = await get_calendar_adapter(tid, employee_id)
    except Exception as exc:  # noqa: BLE001
        logger.info("Geplante Gespräche: kein Kalender-Adapter (%s)", exc)
        return []

    heute = dt.datetime.now().date()
    tage_liste = [heute + dt.timedelta(days=i) for i in range(max(1, tage))]
    ergebnisse = await asyncio.gather(
        *[adapter.list_events_for_day(tag) for tag in tage_liste],
        return_exceptions=True,
    )

    jetzt = dt.datetime.now()
    events: list[dict] = []
    for ergebnis in ergebnisse:
        if isinstance(ergebnis, BaseException):
            logger.info("Geplante Gespräche: Tagesabruf gescheitert: %s", ergebnis)
            continue
        for ev in ergebnis or []:
            start = ev.get("start_dt")
            if not isinstance(start, dt.datetime) or start < jetzt:
                continue  # vorbei — steht schon in der Historie
            events.append({
                "start": start,
                "event_id": (ev.get("event_id") or "").strip(),
                "titel": (ev.get("subject") or "").strip() or "Termin",
                "ort": (ev.get("location") or "").strip(),
            })
    events.sort(key=lambda e: e["start"])
    return events


@router.get("/gespraeche/geplant")
async def api_gespraeche_geplant(
    request: Request, emp: Employee = Depends(require_app_user),
) -> JSONResponse:
    """Was als Nächstes ansteht — bevor das Gespräch überhaupt existiert.

    Zwei Quellen, eine Liste:
    * ``kalender`` — Termine aus dem Kalender des Mitarbeiters (auch die,
      die Q am Telefon gebucht hat). Antippen startet das Gespräch dazu.
    * ``gespraech`` — schon angelegte Gespräche mit Termin in der Zukunft.
      Antippen öffnet den Arbeitsbereich.

    Termine, zu denen bereits ein Gespräch läuft, erscheinen nur einmal:
    der Kalender-Zweig lässt sie über ``kalender_event_id`` weg.
    """
    tid = current_tenant_id(request)

    async with get_session() as s:
        offen = (await s.execute(
            select(Kundengespraech)
            .where(Kundengespraech.tenant_id == tid)
            .where(Kundengespraech.termin_datum.is_not(None))
            .where(Kundengespraech.termin_datum >= dt.datetime.now())
            .where(Kundengespraech.status.not_in(
                [KUNDENGESPRAECH_STATUS_ABGELEHNT,
                 KUNDENGESPRAECH_STATUS_ABGESCHLOSSEN]))
            .order_by(Kundengespraech.termin_datum.asc())
            .limit(50)
        )).scalars().all()
    belegt = {(g.kalender_event_id or "").strip() for g in offen if g.kalender_event_id}

    geplant = [{
        "quelle": "gespraech",
        "id": str(g.id),
        "kunde": g.kunde_name,
        "titel": g.kunde_name,
        "ort": g.termin_ort or "",
        "zeit": _fmt_dt(g.termin_datum),
        "termin_iso": g.termin_datum.isoformat() if g.termin_datum else "",
        "event_id": g.kalender_event_id or "",
    } for g in offen]

    for ev in await _geplante_kalendertermine(tid, emp.id, _GESPRAECH_GEPLANT_TAGE):
        if ev["event_id"] and ev["event_id"] in belegt:
            continue
        geplant.append({
            "quelle": "kalender",
            "id": "",
            "kunde": _kunde_aus_betreff(ev["titel"]),
            "titel": ev["titel"],
            "ort": ev["ort"],
            "zeit": _fmt_dt(ev["start"]),
            "termin_iso": ev["start"].isoformat(),
            "event_id": ev["event_id"],
        })

    geplant.sort(key=lambda x: x.get("termin_iso") or "")
    return JSONResponse({"geplant": geplant})


@router.get("/gespraeche/{gespraech_id}")
async def api_gespraech_detail(
    gespraech_id: str, request: Request, _e=Depends(require_app_user),
) -> JSONResponse:
    """Alles zu einem Gespräch: Kundendaten, Diktat-Ergebnis, Handnotiz,
    Bilder, Termin."""
    tid = current_tenant_id(request)
    try:
        gid = uuid.UUID(gespraech_id)
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "ungueltige id"}, status_code=400)
    g, dateien = await _gespraech_laden(tid, gid)
    if g is None:
        return JSONResponse({"ok": False, "error": "nicht gefunden"}, status_code=404)

    dauer = ""
    if g.audio_dauer_sekunden:
        m, sec = divmod(int(g.audio_dauer_sekunden), 60)
        dauer = f"{m}:{sec:02d} min"
    return JSONResponse({
        "ok": True,
        "id": str(g.id),
        "zeit": _fmt_dt(g.gespraech_datum),
        "dauer": dauer,
        "kunde": await _kunden_kontext(tid, g),
        "briefing": g.briefing_kurz or "",
        "notizen": g.notizen_lang or "",
        "handnotiz": g.handnotiz or "",
        "todos": list(g.todos or []),
        "transkript": g.raw_transcript or "",
        "termin": _fmt_dt(g.termin_datum) if g.termin_datum else "",
        "termin_ort": g.termin_ort or "",
        "bilder": [_datei_zeile(d) for d in dateien],
        "status": g.status,
        "abgeschlossen": g.status == KUNDENGESPRAECH_STATUS_ABGESCHLOSSEN,
        "abgeschlossen_am": _fmt_dt(g.abgeschlossen_am),
        "protokoll_url": g.protokoll_drive_url or "",
    })


@router.post("/gespraeche/{gespraech_id}/abschliessen")
async def api_gespraech_abschliessen(
    gespraech_id: str,
    request: Request,
    emp: Employee = Depends(require_app_user),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """„Fertig — beim Kunden einpflegen": das Ende des Arbeitsbereichs.

    Legt den Kunden an, falls es ihn im Kundenstamm noch nicht gibt, und
    schreibt Protokoll + Bilder in seinen Drive-Ordner (siehe
    core/services/gespraech_abschluss.py). Ohne Drive-Verbindung gilt das
    Gespräch trotzdem als eingepflegt — dann kommt ein ``hinweis`` zurück.
    """
    from core.services.gespraech_abschluss import schliesse_gespraech_ab
    tid = current_tenant_id(request)
    try:
        gid = uuid.UUID(gespraech_id)
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "ungueltige id"}, status_code=400)

    ergebnis = await schliesse_gespraech_ab(tid, gid, employee_id=emp.id)
    if not ergebnis.get("ok"):
        return JSONResponse(ergebnis, status_code=404)
    return JSONResponse(ergebnis)


@router.post("/gespraeche/{gespraech_id}/verwerfen")
async def api_gespraech_verwerfen(
    gespraech_id: str,
    request: Request,
    _e=Depends(require_app_user),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Gespräch lief schlecht — weg damit.

    Soft-Delete über den bestehenden Status ``abgelehnt``: überall
    ausgeblendet (Liste, Termine, Beratungs-Leads), die Zeile bleibt aber
    stehen. Ein bereits eingepflegtes Gespräch lässt sich nicht mehr
    verwerfen — die Daten liegen dann schon beim Kunden.
    """
    tid = current_tenant_id(request)
    try:
        gid = uuid.UUID(gespraech_id)
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "ungueltige id"}, status_code=400)
    async with get_session() as s:
        g = (await s.execute(
            select(Kundengespraech).where(
                Kundengespraech.id == gid, Kundengespraech.tenant_id == tid)
        )).scalar_one_or_none()
        if g is None:
            return JSONResponse({"ok": False, "error": "nicht gefunden"}, status_code=404)
        if g.status == KUNDENGESPRAECH_STATUS_ABGESCHLOSSEN:
            return JSONResponse(
                {"ok": False, "error": "Schon beim Kunden eingepflegt."},
                status_code=409)
        g.status = KUNDENGESPRAECH_STATUS_VERWORFEN
        await s.commit()
    logger.info("PWA-Gespräch verworfen: id=%s tenant=%s", gid, tid)
    return JSONResponse({"ok": True})


@router.post("/gespraeche/{gespraech_id}/notiz")
async def api_gespraech_notiz(
    gespraech_id: str,
    request: Request,
    _e=Depends(require_app_user),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Handnotiz speichern (ersetzt die bisherige). Bleibt INTERN — sie
    geht nie in eine Kundenmail."""
    tid = current_tenant_id(request)
    try:
        gid = uuid.UUID(gespraech_id)
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "ungueltige id"}, status_code=400)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "Ungueltige Daten."}, status_code=400)
    text = (body.get("text") or "").strip()[:_GESPRAECH_MAX_NOTIZ]
    async with get_session() as s:
        g = (await s.execute(
            select(Kundengespraech).where(
                Kundengespraech.id == gid, Kundengespraech.tenant_id == tid)
        )).scalar_one_or_none()
        if g is None:
            return JSONResponse({"ok": False, "error": "nicht gefunden"}, status_code=404)
        g.handnotiz = text or None
        await s.commit()
    return JSONResponse({"ok": True})


@router.post("/gespraeche/{gespraech_id}/foto")
async def api_gespraech_foto(
    gespraech_id: str,
    request: Request,
    emp: Employee = Depends(require_app_user),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Foto zum Gespräch: landet im Drive-Kundenordner (wie das übrige
    Archiv) und wird dem Gespräch zugeordnet. Body = rohe Bild-Bytes."""
    from core.integrations.google_drive import upload_file_to_kunde_folder
    from core.models.gespraech_datei import GespraechDatei, GESPRAECH_DATEI_FOTO
    tid = current_tenant_id(request)
    try:
        gid = uuid.UUID(gespraech_id)
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "ungueltige id"}, status_code=400)

    daten = await request.body()
    if not daten:
        return JSONResponse({"ok": False, "error": "Kein Foto empfangen."}, status_code=400)
    if len(daten) > _GESPRAECH_MAX_FOTO_BYTES:
        mb = len(daten) // 1024 // 1024
        return JSONResponse(
            {"ok": False, "error": f"Foto zu gross ({mb} MB, max 15 MB)."},
            status_code=413)
    mime = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if mime not in _GESPRAECH_FOTO_MIMES:
        return JSONResponse(
            {"ok": False, "error": "Nur JPEG-, PNG- oder WebP-Fotos."}, status_code=415)

    g, _ = await _gespraech_laden(tid, gid)
    if g is None:
        return JSONResponse({"ok": False, "error": "nicht gefunden"}, status_code=404)

    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
    name = f"gespraech_{ts}{_GESPRAECH_FOTO_MIMES[mime]}"
    try:
        res = await upload_file_to_kunde_folder(
            tenant_id=tid, kunde_name=g.kunde_name, file_bytes=daten,
            filename=name, mime_type=mime, employee_id=emp.id,
        )
    except ValueError:
        return JSONResponse(
            {"ok": False, "error": "Google Drive ist nicht verbunden. Bitte in den Einstellungen verbinden."},
            status_code=409)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Gespräch-Foto-Upload fehlgeschlagen: %s", exc)
        return JSONResponse(
            {"ok": False, "error": "Upload fehlgeschlagen. Bitte erneut versuchen."},
            status_code=502)

    async with get_session() as s:
        d = GespraechDatei(
            tenant_id=tid, gespraech_id=gid, typ=GESPRAECH_DATEI_FOTO,
            drive_file_id=res.get("file_id"), drive_url=res.get("web_link"),
            dateiname=name, mime=mime,
        )
        s.add(d)
        await s.flush()
        zeile = _datei_zeile(d)
        await s.commit()
    logger.info("PWA-Gespräch-Foto: gespraech=%s tenant=%s", gid, tid)
    return JSONResponse({"ok": True, "bild": zeile})


@router.post("/gespraeche/{gespraech_id}/diktat")
async def api_gespraech_diktat(
    gespraech_id: str,
    request: Request,
    emp: Employee = Depends(require_app_user),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Diktat in ein bestehendes Gespräch. Gleiche Analyse wie
    ``/aufnahmen/diktat``, aber der Kunde steht schon fest.

    Mehrfaches Diktieren hängt an, statt zu überschreiben — wer auf der
    Baustelle zweimal spricht, will nichts verlieren. Der Kundenname aus
    der KI wird bewusst ignoriert: gewählt hat ihn der Handwerker."""
    from core.ai import analyse_kundengespraech_from_audio

    tid = current_tenant_id(request)
    try:
        gid = uuid.UUID(gespraech_id)
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "ungueltige id"}, status_code=400)

    audio_bytes = await request.body()
    err = _validate_diktat_audio(audio_bytes)
    if err:
        return JSONResponse({"ok": False, "error": err[0]}, status_code=err[1])
    mime = _normalize_diktat_mime(request.headers.get("content-type"))
    if mime is None:
        return JSONResponse(
            {"ok": False, "error": "Audioformat wird nicht unterstuetzt."},
            status_code=415)
    dauer = _parse_diktat_duration(request.headers.get("x-audio-duration"))

    g, _ = await _gespraech_laden(tid, gid)
    if g is None:
        return JSONResponse({"ok": False, "error": "nicht gefunden"}, status_code=404)

    try:
        extracted = await analyse_kundengespraech_from_audio(
            audio_bytes, mime_type=mime, tenant_id=tid)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Gespräch-Diktat: Gemini-Analyse fehlgeschlagen: %s", exc)
        return JSONResponse(
            {"ok": False, "error": "Analyse fehlgeschlagen. Bitte erneut versuchen."},
            status_code=502)

    def _anhaengen(alt: str | None, neu: str | None) -> str | None:
        alt, neu = (alt or "").strip(), (neu or "").strip()
        if not neu:
            return alt or None
        return f"{alt}\n\n{neu}" if alt else neu

    async with get_session() as s:
        row = (await s.execute(
            select(Kundengespraech).where(
                Kundengespraech.id == gid, Kundengespraech.tenant_id == tid)
        )).scalar_one_or_none()
        if row is None:
            return JSONResponse({"ok": False, "error": "nicht gefunden"}, status_code=404)
        row.briefing_kurz = _anhaengen(row.briefing_kurz, extracted.get("briefing_kurz"))
        row.notizen_lang = _anhaengen(row.notizen_lang, extracted.get("notizen_lang"))
        row.raw_transcript = _anhaengen(row.raw_transcript, extracted.get("transcript"))
        neue_todos = [t for t in (extracted.get("todos") or []) if str(t).strip()]
        if neue_todos:
            row.todos = list(row.todos or []) + neue_todos
        if dauer:
            row.audio_dauer_sekunden = (row.audio_dauer_sekunden or 0) + dauer
        # Termin nur setzen, wenn noch keiner steht — ein zweites Diktat
        # soll einen bereits vereinbarten Termin nicht still verschieben.
        if row.termin_datum is None:
            termin = _parse_diktat_termin(extracted.get("termin_datum"))
            if termin:
                row.termin_datum = termin
                row.termin_ort = extracted.get("termin_ort")
        if not row.confidence:
            row.confidence = extracted.get("extraction_confidence")
        await s.commit()

    from core.models.app_usage_event import record_app_usage, USAGE_DIKTAT
    await record_app_usage(tid, emp.id, USAGE_DIKTAT)
    logger.info("PWA-Gespräch-Diktat: gespraech=%s tenant=%s todos=%d",
                gid, tid, len(extracted.get("todos") or []))
    return JSONResponse({
        "ok": True,
        "briefing": (extracted.get("briefing_kurz") or "")[:300],
        "todos": list(extracted.get("todos") or []),
    })


@router.post("/gespraeche/{gespraech_id}/mail")
async def api_gespraech_mail(
    gespraech_id: str,
    request: Request,
    emp: Employee = Depends(require_app_user),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Baut den Kundenmail-Entwurf zum Gespräch.

    Body: {"bild_ids": [...]} — welche Bilder mitgehen sollen. Der Entwurf
    enthält NUR die Zusammenfassung und die gewählten Bilder; Transkript,
    Handnotiz und To-dos bleiben im Betrieb (siehe
    core/services/kundengespraech.py). Antwort ist eine Entwurfs-Karte, die
    der Handwerker im Assistenten redigiert und freigibt."""
    from core.services.kundengespraech import baue_kundenmail
    tid = current_tenant_id(request)
    try:
        gid = uuid.UUID(gespraech_id)
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "ungueltige id"}, status_code=400)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    gewaehlt = {str(x) for x in (body.get("bild_ids") or [])}

    g, dateien = await _gespraech_laden(tid, gid)
    if g is None:
        return JSONResponse({"ok": False, "error": "nicht gefunden"}, status_code=404)
    bilder = [d for d in dateien if str(d.id) in gewaehlt]

    tenant = getattr(request.state, "app_tenant", None)
    betrieb = getattr(tenant, "company_name", "") or ""
    entwurf = await baue_kundenmail(
        tid, gespraech=g, bilder=bilder, betrieb=betrieb, employee_id=emp.id)
    logger.info("PWA-Gespräch-Mail-Entwurf: gespraech=%s tenant=%s bilder=%d",
                gid, tid, len(bilder))
    return JSONResponse({"ok": True, "entwurf": entwurf})


@router.get("/kunden/profil")
async def api_kunde_profil(
    request: Request, name: str = "", kunde_id: str = "",
    _e=Depends(require_app_user),
) -> JSONResponse:
    """Gebuendeltes Kundenprofil: Gespraeche, Angebote, Rechnungen +
    Drive-Ordner. Read-only, tenant-gescoped.

    Aufloesung (Phase 5): mit `kunde_id` praezise ueber den
    Kundenstamm; sonst ueber den (exakten) Namen — dann laufen die
    Abfragen ueber kunde_id ALLER namensgleichen Kunden plus
    Namens-ilike als Uebergangs-Fallback fuer Zeilen ohne kunde_id
    (faellt in Phase 7 weg).
    """
    from sqlalchemy import or_
    from core.models import Kunde
    from core.models.tenant_kunde_drive import TenantKundeDrive
    from core.services.kunde_identity import (
        find_kunden_by_name, kunde_anzeige_merkmal)
    tid = current_tenant_id(request)
    nm = (name or "").strip()
    kid_raw = (kunde_id or "").strip()
    if not kid_raw and len(nm) < 2:
        return JSONResponse({"ok": False, "error": "Name fehlt."}, status_code=400)
    async with get_session() as s:
        kunde = None
        kunden_ids: list = []
        name_fallback = True
        if kid_raw:
            try:
                kid = uuid.UUID(kid_raw)
            except ValueError:
                return JSONResponse(
                    {"ok": False, "error": "kunde_id ungueltig."},
                    status_code=400)
            kunde = await s.get(Kunde, kid)
            if kunde is None or kunde.tenant_id != tid:
                return JSONResponse(
                    {"ok": False, "error": "Kunde nicht gefunden."},
                    status_code=404)
            nm = kunde.name
            kunden_ids = [kunde.id]
            # Praeziser Modus: kein Namens-Fallback, sonst wuerden
            # Zeilen eines namensgleichen ANDEREN Kunden einsickern.
            name_fallback = False
        else:
            treffer = await find_kunden_by_name(s, tid, nm)
            kunden_ids = [k.id for k in treffer]
            if len(treffer) == 1:
                kunde = treffer[0]

        # Zusammenfuehren-Karte: weitere Kunden mit exakt diesem Namen,
        # je mit Unterscheidungsmerkmal. Nur wenn das Profil eindeutig
        # aufgeloest ist — sonst gaebe es keine klare Merge-Richtung.
        dubletten = []
        if kunde is not None:
            alle = await find_kunden_by_name(s, tid, kunde.name)
            # Kontaktdaten je Dublette mitgeben: der Merge-Dialog laesst
            # bei abweichenden Werten pro Feld die Hauptdaten waehlen.
            dubletten = [
                {"id": str(k.id), "merkmal": kunde_anzeige_merkmal(k),
                 "email": k.email, "telefon": k.telefon,
                 "adresse": k.adresse}
                for k in alle if k.id != kunde.id
            ]

        def _cond(model):
            conds = [model.kunde_name.ilike(nm)] if name_fallback else []
            if kunden_ids:
                conds.append(model.kunde_id.in_(kunden_ids))
            return or_(*conds)

        g = (await s.execute(
            select(Kundengespraech)
            .where(Kundengespraech.tenant_id == tid, _cond(Kundengespraech))
            .order_by(Kundengespraech.gespraech_datum.desc()).limit(25)
        )).scalars().all()
        a = (await s.execute(
            select(Angebot)
            .where(Angebot.tenant_id == tid, _cond(Angebot))
            .order_by(Angebot.created_at.desc()).limit(25)
        )).scalars().all()
        r = (await s.execute(
            select(Rechnung)
            .where(Rechnung.tenant_id == tid, _cond(Rechnung))
            .order_by(Rechnung.created_at.desc()).limit(25)
        )).scalars().all()
        drv = (await s.execute(
            select(TenantKundeDrive)
            .where(TenantKundeDrive.tenant_id == tid, _cond(TenantKundeDrive))
            # non-null kunde_id zuerst — der praezise Treffer gewinnt.
            # Haengen nach einem Merge mehrere Ordner am Kunden, gewinnt
            # der meistgenutzte (die Dateiliste aggregiert ohnehin alle).
            .order_by(TenantKundeDrive.kunde_id.is_(None),
                      TenantKundeDrive.upload_count.desc())
            .limit(1)
        )).scalars().first()

    email = (kunde.email if kunde and kunde.email else "") or next(
        (x.kunde_email for x in a if getattr(x, "kunde_email", None)), "") or ""
    drive = None
    if drv:
        drive = {
            "url": drv.drive_folder_url,
            "anzahl": drv.upload_count,
            "letzter": _fmt_dt(drv.last_upload_at),
        }
    return JSONResponse({
        "ok": True,
        "name": nm,
        "email": email,
        # Phase 5: Kundenstamm-Infos (additiv; None wenn der Name
        # mehrdeutig ist oder kein Kunde existiert)
        "kunde_id": str(kunde.id) if kunde else None,
        "telefon": (kunde.telefon if kunde else None) or None,
        "adresse": (kunde.adresse if kunde else None) or None,
        "merkmal": kunde_anzeige_merkmal(kunde) if kunde else None,
        # Reiner Stammdaten-Wert fuer den Merge-Dialog: "email" oben
        # kann ein Angebots-Fallback sein und taugt nicht als
        # Vergleichsbasis dafuer, was am Kunden wirklich steht.
        "email_stamm": (kunde.email if kunde else None) or None,
        "namensgleiche_kunden": len(kunden_ids),
        # Zusammenfuehren-Karte (Inhaber-Aktion, POST /kunden/merge)
        "dubletten": dubletten,
        "gespraeche": [{"id": str(x.id), "briefing": (x.briefing_kurz or "")[:160],
                        "zeit": _fmt_dt(x.gespraech_datum)} for x in g],
        "angebote": [{"betrag": _fmt_eur(x.gesamtbetrag_brutto_eur),
                      "status": _label(_ANGEBOT_LABELS, x.status)[0],
                      "pill": _label(_ANGEBOT_LABELS, x.status)[1],
                      "zeit": _fmt_dt(x.created_at)} for x in a],
        "rechnungen": [{"betrag": _fmt_eur(x.betrag_brutto_eur),
                        "nummer": x.lexware_voucher_number or "",
                        "status": _label(_RECHNUNG_LABELS, x.status)[0],
                        "pill": _label(_RECHNUNG_LABELS, x.status)[1],
                        "zeit": _fmt_dt(x.created_at)} for x in r],
        "drive": drive,
    })


@router.post("/kunden/merge")
async def api_kunden_merge(
    request: Request,
    _e=Depends(require_app_inhaber),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Zusammenfuehren-Karte im Kundenprofil: die Quelle geht im Ziel
    auf (body: {"quelle_id": "...", "ziel_id": "..."}). Kernlogik in
    core/services/kunde_merge.py — dieselben Regeln wie das CLI-Skript
    scripts/merge_kunden.py (additive-only, Ref-Konflikte brechen ab).

    Inhaber-only: das Zusammenfuehren ist praktisch nicht rueckgaengig
    zu machen und gehoert nicht in die Monteur-Sicht.
    """
    from core.models import Kunde
    from core.services.kunde_identity import (
        find_kunden_by_name, kunde_anzeige_merkmal)
    from core.services.kunde_merge import MergeAbbruch, merge_kunden
    tid = current_tenant_id(request)
    body = await request.json()
    try:
        quelle_id = uuid.UUID((body.get("quelle_id") or "").strip())
        ziel_id = uuid.UUID((body.get("ziel_id") or "").strip())
    except (ValueError, TypeError, AttributeError):
        return JSONResponse(
            {"ok": False, "error": "quelle_id/ziel_id ungueltig."},
            status_code=400)
    # Hauptdaten-Wahl aus dem Dialog: pro Feld "ziel" (Default) oder
    # "quelle". Unbekannte Felder/Werte werden ignoriert — dann greift
    # die Default-Regel (Ziel gewinnt, Luecken fuellen).
    feld_wahl: dict[str, str] = {}
    felder_raw = body.get("felder")
    if isinstance(felder_raw, dict):
        for feld, wahl in felder_raw.items():
            if feld in ("name", "email", "telefon", "adresse") \
                    and wahl in ("quelle", "ziel"):
                feld_wahl[feld] = wahl
    async with get_session() as s:
        # Tenant-Gate VOR dem Service: der prueft nur, dass Quelle und
        # Ziel im selben Tenant liegen — nicht, dass es UNSERER ist.
        for kid in (quelle_id, ziel_id):
            k = await s.get(Kunde, kid)
            if k is None or k.tenant_id != tid:
                return JSONResponse(
                    {"ok": False, "error": "Kunde nicht gefunden."},
                    status_code=404)
        try:
            e = await merge_kunden(
                s, quelle_id, ziel_id, feld_wahl=feld_wahl)
        except MergeAbbruch as exc:
            return JSONResponse(
                {"ok": False, "error": str(exc)}, status_code=409)
        ziel = e.ziel
        # Der Mensch hat die Dublette gerade aufgeloest: bleibt kein
        # weiterer namensgleicher Kunde uebrig, ist auch das
        # Review-Flag am Ziel erledigt. Bei weiteren Dubletten bleibt
        # es stehen (dieselbe Zurueckhaltung wie --clear-review im CLI).
        if ziel.needs_review:
            verbliebene = await find_kunden_by_name(s, tid, ziel.name)
            if len(verbliebene) <= 1:
                ziel.needs_review = False
        antwort = {
            "ok": True,
            "kunde_id": str(ziel.id),
            "name": ziel.name,
            "merkmal": kunde_anzeige_merkmal(ziel),
            "umgehaengt": e.umgehaengt,
            "schon_gemergt": e.schon_gemergt,
            # >1 Drive-Ordner am Ziel: Dateien liegen jetzt verteilt
            "drive_hinweis": len(e.drive_keys) > 1,
        }
        await s.commit()
    logger.info("PWA-Kunden-Merge: quelle=%s -> ziel=%s tenant=%s",
                quelle_id, antwort["kunde_id"], tid)
    return JSONResponse(antwort)


# =====================================================================
# Kunden-Archiv: Dateien/Notizen in den Drive-Ordner des Kunden ablegen
#
# Telegram-Paritaet zum /archiv-Wizard. Wiederverwendung:
# upload_file_to_kunde_folder (google_drive.py) legt den Kunden-Ordner
# race-safe an bzw. findet ihn (TenantKundeDrive) und zaehlt upload_count
# hoch — hier liegt nur der App-Upload-Endpoint im Belege-Muster (rohe
# Bytes + Content-Type, KEIN multipart). require_app_user (Monteur im Feld,
# kein Inhaber-Gate). Feature-gegated: drive_archiv.
# =====================================================================

_ARCHIV_ALLOWED_MIMES = {"image/jpeg", "image/png", "image/webp", "application/pdf"}
_ARCHIV_MAX_SIZE_BYTES = 25_000_000  # 25 MB (wie Telegram-/archiv)
_ARCHIV_EXT = {
    "image/jpeg": ".jpg", "image/png": ".png",
    "image/webp": ".webp", "application/pdf": ".pdf",
}


def _normalize_archiv_mime(raw: str | None) -> str | None:
    mime = (raw or "").split(";")[0].strip().lower()
    return mime if mime in _ARCHIV_ALLOWED_MIMES else None


def _archiv_note_blob(kunde_name: str, text: str) -> bytes:
    """Text-Notiz als .txt mit Kopfzeile (Kunde + Zeitstempel) — spiegelt den
    Telegram-Notiz-Header."""
    ts = dt.datetime.now(dt.timezone.utc).strftime("%d.%m.%Y %H:%M")
    header = f"Notiz für {kunde_name}\nErfasst: {ts} UTC\n" + ("-" * 40) + "\n\n"
    return (header + text).encode("utf-8")


async def _archiv_feature_ok(tid) -> bool:
    from core.features.check import is_feature_enabled
    return await is_feature_enabled(tid, "drive_archiv")


@router.post("/archiv/upload")
async def api_archiv_upload(
    request: Request,
    emp: Employee = Depends(require_app_user),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Foto/PDF aus der PWA in den Drive-Ordner eines Kunden ablegen.

    Body = rohe Datei-Bytes; Content-Type bestimmt den MIME. Query:
    ?kunde_name= (Pflicht), ?filename=, ?kunde_email= (verbessert das
    Ordner-Matching), optional ?caption= legt zusaetzlich eine Text-Notiz
    in denselben Ordner."""
    from core.integrations.google_drive import upload_file_to_kunde_folder
    tid = current_tenant_id(request)
    if not await _archiv_feature_ok(tid):
        return JSONResponse({"ok": False, "error": "Das Kunden-Archiv ist nicht aktiv."}, status_code=403)

    file_bytes = await request.body()
    if not file_bytes:
        return JSONResponse({"ok": False, "error": "Keine Datei empfangen."}, status_code=400)
    if len(file_bytes) > _ARCHIV_MAX_SIZE_BYTES:
        mb = len(file_bytes) // 1024 // 1024
        return JSONResponse({"ok": False, "error": f"Datei zu gross ({mb} MB, max 25 MB)."}, status_code=413)
    mime = _normalize_archiv_mime(request.headers.get("content-type"))
    if mime is None:
        return JSONResponse({"ok": False, "error": "Nur JPEG, PNG, WebP oder PDF erlaubt."}, status_code=415)

    kunde_name = (request.query_params.get("kunde_name") or "").strip()[:200]
    if len(kunde_name) < 2:
        return JSONResponse({"ok": False, "error": "Kunde fehlt."}, status_code=400)
    kunde_email = (request.query_params.get("kunde_email") or "").strip()[:200] or None
    caption = (request.query_params.get("caption") or "").strip()[:1000] or None
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = (
        (request.query_params.get("filename") or "").strip()[:255]
        or f"foto_{ts}{_ARCHIV_EXT.get(mime, '.bin')}"
    )

    try:
        result = await upload_file_to_kunde_folder(
            tenant_id=tid, kunde_name=kunde_name, file_bytes=file_bytes,
            filename=filename, mime_type=mime, employee_id=emp.id,
            kunde_email=kunde_email,
        )
        if caption:
            # Notiz best-effort in denselben Ordner — der Datei-Upload oben
            # bleibt auch dann gueltig, wenn die Notiz scheitert.
            try:
                await upload_file_to_kunde_folder(
                    tenant_id=tid, kunde_name=kunde_name,
                    file_bytes=_archiv_note_blob(kunde_name, caption),
                    filename=f"notiz_{ts}.txt", mime_type="text/plain",
                    employee_id=emp.id, kunde_email=kunde_email,
                )
            except Exception:  # noqa: BLE001
                logger.warning("Archiv-Notiz neben Datei fehlgeschlagen (tenant=%s)", tid)
    except ValueError as e:
        # get_drive_service wirft ValueError wenn kein Drive-Scope verbunden ist
        logger.info("Archiv-Upload ohne Drive-Verbindung (tenant=%s): %s", tid, e)
        return JSONResponse(
            {"ok": False, "error": "Google Drive ist nicht verbunden. Bitte in den Einstellungen verbinden."},
            status_code=409,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("PWA-Archiv-Upload fehlgeschlagen: %s", e)
        return JSONResponse({"ok": False, "error": "Drive-Upload fehlgeschlagen. Bitte erneut versuchen."}, status_code=502)

    logger.info("PWA-Archiv-Upload: tenant=%s mitarbeiter=%s kunde=%s mime=%s", tid, emp.id, kunde_name, mime)
    return JSONResponse({
        "ok": True,
        "folder_url": result.get("kunde_folder_url"),
        "upload_count": result.get("upload_count"),
    })


@router.post("/archiv/notiz")
async def api_archiv_notiz(
    request: Request,
    emp: Employee = Depends(require_app_user),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Reine Text-Notiz in den Kunden-Drive-Ordner.
    Body JSON: { kunde_name, text, kunde_email? }."""
    from core.integrations.google_drive import upload_file_to_kunde_folder
    tid = current_tenant_id(request)
    if not await _archiv_feature_ok(tid):
        return JSONResponse({"ok": False, "error": "Das Kunden-Archiv ist nicht aktiv."}, status_code=403)
    body = await request.json()
    kunde_name = (body.get("kunde_name") or "").strip()[:200]
    text = (body.get("text") or "").strip()
    if len(kunde_name) < 2:
        return JSONResponse({"ok": False, "error": "Kunde fehlt."}, status_code=400)
    if len(text) < 2:
        return JSONResponse({"ok": False, "error": "Notiz ist leer."}, status_code=400)
    kunde_email = (body.get("kunde_email") or "").strip()[:200] or None
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
    try:
        result = await upload_file_to_kunde_folder(
            tenant_id=tid, kunde_name=kunde_name,
            file_bytes=_archiv_note_blob(kunde_name, text),
            filename=f"notiz_{ts}.txt", mime_type="text/plain",
            employee_id=emp.id, kunde_email=kunde_email,
        )
    except ValueError as e:
        logger.info("Archiv-Notiz ohne Drive-Verbindung (tenant=%s): %s", tid, e)
        return JSONResponse(
            {"ok": False, "error": "Google Drive ist nicht verbunden. Bitte in den Einstellungen verbinden."},
            status_code=409,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("PWA-Archiv-Notiz fehlgeschlagen: %s", e)
        return JSONResponse({"ok": False, "error": "Notiz konnte nicht abgelegt werden."}, status_code=502)
    logger.info("PWA-Archiv-Notiz: tenant=%s mitarbeiter=%s kunde=%s", tid, emp.id, kunde_name)
    return JSONResponse({
        "ok": True,
        "folder_url": result.get("kunde_folder_url"),
        "upload_count": result.get("upload_count"),
    })


# =====================================================================
# Kunden-Archiv: Dateien aus Drive lesen + Proxy
# =====================================================================

_DRIVE_FILE_ID_RE = __import__("re").compile(r"^[A-Za-z0-9_\-]{10,200}$")


@router.get("/archiv/dateien")
async def api_archiv_dateien(
    request: Request,
    _e=Depends(require_app_user),
    kunde_name: str = "",
) -> JSONResponse:
    """Listet Dateien im Drive-Ordner eines Kunden (neueste zuerst).

    Query: ?kunde_name=
    Gibt leere Liste zurueck wenn kein Ordner existiert oder Drive nicht
    verbunden — nie ein Fehler der das Profil kaputt machen koennte."""
    from core.integrations.google_drive import list_files_in_kunde_folder
    tid = current_tenant_id(request)
    if not await _archiv_feature_ok(tid):
        return JSONResponse({"ok": True, "dateien": []})
    nm = (kunde_name or "").strip()[:200]
    if len(nm) < 2:
        return JSONResponse({"ok": False, "error": "Kunde fehlt."}, status_code=400)
    dateien = await list_files_in_kunde_folder(tid, nm)
    return JSONResponse({"ok": True, "dateien": dateien})


@router.get("/archiv/datei/{file_id}")
async def api_archiv_datei_proxy(
    file_id: str,
    request: Request,
    _e=Depends(require_app_user),
    thumb: int = 0,
) -> Response:
    """Proxy-Endpunkt: Datei-Bytes aus Drive durchreichen (max 10 MB).

    Mit ``?thumb=1`` liefert Drive statt des Originals sein Vorschaubild
    (~50 KB statt bis zu 10 MB) — das nutzt die Archiv-Galerie fuer ihre
    Kacheln. Hat Drive kein Thumbnail, faellt es aufs Original zurueck.

    Authentifizierung via Session (require_app_user) — kein direkter
    Drive-Link im Browser noetig. Cache-Control: private, 1h."""
    from core.integrations.google_drive import get_file_bytes, get_thumbnail_bytes
    tid = current_tenant_id(request)
    if not await _archiv_feature_ok(tid):
        return JSONResponse({"ok": False}, status_code=403)
    if not _DRIVE_FILE_ID_RE.match(file_id):
        return JSONResponse({"ok": False}, status_code=400)
    try:
        data = mime = None
        if thumb:
            got = await get_thumbnail_bytes(tid, file_id)
            if got:
                data, mime = got
        if data is None:
            data, mime = await get_file_bytes(tid, file_id)
    except ValueError:
        return JSONResponse(
            {"ok": False, "error": "Drive nicht verbunden."},
            status_code=409,
        )
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=413)
    except Exception:
        logger.exception("Archiv-Datei-Proxy fehlgeschlagen: file_id=%s tenant=%s", file_id, tid)
        return JSONResponse(
            {"ok": False, "error": "Datei konnte nicht geladen werden."},
            status_code=502,
        )
    # Thumbnails aendern sich nie (Drive-Datei ist immutable) -> lange
    # cachen, damit ein zweiter Besuch der Galerie gar nicht erst zu Drive
    # muss. Originale bleiben bei 1h.
    max_age = 86400 if thumb else 3600
    return Response(
        content=data,
        media_type=mime,
        headers={"Cache-Control": f"private, max-age={max_age}"},
    )


# =====================================================================
# Wissensdatenbank (lesen / anlegen / löschen)
# =====================================================================

@router.get("/wissen")
async def api_wissen(request: Request, _e=Depends(require_app_user)) -> JSONResponse:
    tid = current_tenant_id(request)
    async with get_session() as s:
        rows = (await s.execute(
            select(TenantKnowledge).where(TenantKnowledge.tenant_id == tid)
            .order_by(TenantKnowledge.kategorie, TenantKnowledge.created_at.desc())
        )).scalars().all()
    eintraege = [{
        "id": str(w.id),
        "kategorie": w.kategorie,
        "kategorie_label": KATEGORIE_LABELS.get(w.kategorie, w.kategorie),
        "text": w.text,
    } for w in rows]
    kategorien = [{"key": k, "label": v} for k, v in KATEGORIE_LABELS.items()]
    return JSONResponse({"eintraege": eintraege, "kategorien": kategorien})


@router.post("/wissen")
async def api_wissen_add(
    request: Request, _e=Depends(require_app_user), _c=Depends(require_app_csrf),
) -> JSONResponse:
    tid = current_tenant_id(request)
    body = await request.json() or {}
    kategorie = (body.get("kategorie") or "").strip()
    text = (body.get("text") or "").strip()
    if kategorie not in KATEGORIE_LABELS:
        return JSONResponse({"ok": False, "error": "unbekannte Kategorie"}, status_code=400)
    if not (3 <= len(text) <= 2000):
        return JSONResponse({"ok": False, "error": "Text 3–2000 Zeichen"}, status_code=400)
    async with get_session() as s:
        s.add(TenantKnowledge(tenant_id=tid, kategorie=kategorie, text=text))
    return JSONResponse({"ok": True})


@router.post("/wissen/{wid}/loeschen")
async def api_wissen_delete(
    wid: str, request: Request,
    _e=Depends(require_app_inhaber), _c=Depends(require_app_csrf),
) -> JSONResponse:
    tid = current_tenant_id(request)
    try:
        wid_uuid = uuid.UUID(wid)
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "ungueltige id"}, status_code=400)
    async with get_session() as s:
        w = (await s.execute(
            select(TenantKnowledge)
            .where(TenantKnowledge.id == wid_uuid)
            .where(TenantKnowledge.tenant_id == tid)
        )).scalar_one_or_none()
        if w is None:
            return JSONResponse({"ok": False, "error": "nicht gefunden"}, status_code=404)
        await s.delete(w)
    return JSONResponse({"ok": True})


# =================== Anfragen-Inbox (Welle 2: Telegram-Ersatz) ===================
#
# Datenmodell: EmailConversation (eine pro (Tenant, Kunden-Mail)). Die KI hat
# pro Conversation classification + classification_confidence, der State zeigt
# den Bearbeitungsstand (awaiting_confirmation, proposing_slots, booked,
# closed). Eine Anfrage ist hier eine EmailConversation, deren classification
# RELEVANT_KUNDE oder RELEVANT_GESCHAEFT ist (Privat/Spam ausgefiltert).
#
# UX-Konzept: zwei-Spalten-Layout in der PWA, Inbox links, Detail rechts —
# wie Mail.app auf macOS/iPadOS. Mobile: Inbox first, Detail als Push-Screen.


# Anfrage-Filter — Klassifikationen die wir in der Inbox zeigen. Privat +
# NICHT_RELEVANT bleiben aus damit der Inhaber nicht jeden Spam sieht.
_RELEVANT_CLASSIFICATIONS = (
    "RELEVANT_KUNDE",
    "RELEVANT_GESCHAEFT",
    "UNSICHER",
    None,  # noch nicht klassifiziert — defensiv anzeigen
)


def _classification_label(c: str | None) -> tuple[str, str]:
    """(label, pill-style) — Pill-style passt zu app.css: ok | warn | danger | ""."""
    return {
        "RELEVANT_KUNDE": ("Kunde", "ok"),
        "RELEVANT_GESCHAEFT": ("Geschaeftlich", "ok"),
        "UNSICHER": ("Unsicher", "warn"),
        "PRIVAT": ("Privat", ""),
        "NICHT_RELEVANT": ("Nicht relevant", ""),
    }.get(c or "", ("Neu", "warn"))


def _state_label(state: str) -> tuple[str, str]:
    return {
        "awaiting_confirmation": ("Wartet auf Antwort", "warn"),
        "proposing_slots": ("Slots vorgeschlagen", "warn"),
        "booked": ("Termin gebucht", "ok"),
        "closed": ("Erledigt", ""),
        "storniert": ("Storniert", "danger"),
        "zustellung_fehlgeschlagen": ("Zustellung fehlgeschlagen", "danger"),
        "dialog": ("Im Dialog", "warn"),
    }.get(state, (state, ""))


@router.get("/anfragen")
async def api_anfragen_list(
    request: Request,
    _e=Depends(require_app_user),
) -> JSONResponse:
    """Inbox-Liste der EmailConversations dieses Tenants.

    Sortiert: nicht-geschlossene + juengste zuerst (typische Mail-App-UX).
    Geschlossene werden inkludiert, aber unten — der Inhaber kann sie
    weiter sehen falls Rueckblick gewuenscht.
    """
    tid = current_tenant_id(request)
    async with get_session() as s:
        rows = (await s.execute(
            select(EmailConversation)
            .where(EmailConversation.tenant_id == tid)
            .where(
                (EmailConversation.classification.in_(
                    [c for c in _RELEVANT_CLASSIFICATIONS if c is not None]
                ))
                | (EmailConversation.classification.is_(None))
            )
            .order_by(EmailConversation.updated_at.desc())
            .limit(200)
        )).scalars().all()

    items = []
    for c in rows:
        cls_label, cls_style = _classification_label(c.classification)
        state_label, state_style = _state_label(c.state)
        # Preview = letzte User-Mail (~140 Zeichen), Fallback letzter Q-Reply.
        preview = (c.last_user_message or c.last_q_reply or "").strip()
        if len(preview) > 160:
            preview = preview[:160] + "…"
        items.append({
            "id": str(c.id),
            "kunde_email": c.kunde_email,
            "kunde_name": c.kunde_name or "",
            "subject": c.last_subject or "(kein Betreff)",
            "preview": preview,
            "state": c.state,
            "state_label": state_label,
            "state_style": state_style,
            "classification": c.classification or "",
            "classification_label": cls_label,
            "classification_style": cls_style,
            "termin_datum": c.termin_datum.isoformat() if c.termin_datum else None,
            "drive_folder_url": c.drive_folder_url,
            "updated_at": c.updated_at.isoformat() if c.updated_at else None,
            "updated_at_fmt": _fmt_dt(c.updated_at),
            "closed": c.state == STATE_CLOSED,
        })

    return JSONResponse({"items": items})


@router.get("/anfragen/{anfrage_id}")
async def api_anfrage_detail(
    anfrage_id: str, request: Request,
    _e=Depends(require_app_user),
) -> JSONResponse:
    """Detail einer Anfrage: letzte User-Mail im Klartext + letzte Q-Antwort +
    Klassifikations-Begruendung + Slots-Vorschlaege falls da."""
    tid = current_tenant_id(request)
    try:
        cid = uuid.UUID(anfrage_id)
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "ungueltige id"}, status_code=400)

    async with get_session() as s:
        c = (await s.execute(
            select(EmailConversation)
            .where(EmailConversation.id == cid)
            .where(EmailConversation.tenant_id == tid)  # Tenant-Isolation
        )).scalar_one_or_none()

    if c is None:
        return JSONResponse({"ok": False, "error": "Anfrage nicht gefunden"}, status_code=404)

    # Telefon-Lookup: AnfrageToken mit gleicher kunde_email hat ggf. den
    # vom Voice-Bot oder Formular eingegebenen Phone. Wir nehmen den
    # juengsten (created_at desc) — relevant wenn ein Kunde mit der
    # gleichen Mail verschiedene Tokens haben sollte.
    phone = None
    async with get_session() as s:
        tok = (await s.execute(
            select(AnfrageToken)
            .where(AnfrageToken.tenant_id == tid)
            .where(AnfrageToken.kunde_email == c.kunde_email)
            .where(AnfrageToken.kunde_telefon.is_not(None))
            .order_by(AnfrageToken.created_at.desc())
            .limit(1)
        )).scalar_one_or_none()
        if tok is not None:
            phone = tok.kunde_telefon

    cls_label, cls_style = _classification_label(c.classification)
    state_label, state_style = _state_label(c.state)
    return JSONResponse({
        "id": str(c.id),
        "kunde_email": c.kunde_email,
        "kunde_name": c.kunde_name or "",
        "kunde_telefon": phone or "",
        "subject": c.last_subject or "(kein Betreff)",
        "last_user_message": c.last_user_message or "",
        "last_q_reply": c.last_q_reply or "",
        "classification": c.classification or "",
        "classification_label": cls_label,
        "classification_style": cls_style,
        "classification_reason": c.classification_reason or "",
        "classification_confidence": c.classification_confidence or "",
        "state": c.state,
        "state_label": state_label,
        "state_style": state_style,
        "proposed_slots": c.proposed_slots or [],
        "termin_datum": c.termin_datum.isoformat() if c.termin_datum else None,
        "drive_folder_url": c.drive_folder_url,
        "updated_at_fmt": _fmt_dt(c.updated_at),
        "created_at_fmt": _fmt_dt(c.created_at),
        "closed": c.state == STATE_CLOSED,
    })


# =================== Angebote + Rechnungen (Welle 6) ===================
#
# Wiederverwendet die etablierten Helfer aus core/integrations:
# - extract_angebot_from_text / extract_rechnung_from_text (Gemini)
# - LexwareProvider.create_quotation_draft / create_invoice_draft
# - send_angebot_to_customer / send_rechnung_to_customer (Mail-Pipeline)
#
# UX-Konzept: zweistufiger Flow.
#  Stufe 1: Inhaber tippt OR diktiert Freitext "Parkett 100qm, 4500 Euro"
#           → KI extrahiert Felder → strukturierte Vorschau zur Korrektur
#  Stufe 2: Inhaber editiert nach, klickt "Anlegen" → Lexware-Draft +
#           DB-Insert → Quittung mit Deeplink + Send-Button


async def _build_lexware_provider(tenant_id: uuid.UUID):
    """Inline-Provider-Factory analog zu angebot_mail.py — vermeidet eine
    zirkulaere Abhaengigkeit auf den Telegram-Handler."""
    from core.models.tool_config import ToolConfig
    from core.security.encryption import decrypt
    from core.integrations.lexware import LexwareProvider
    async with get_session() as s:
        tc = (await s.execute(
            select(ToolConfig).where(
                ToolConfig.tenant_id == tenant_id,
                ToolConfig.tool_name == "lexware",
            )
        )).scalar_one_or_none()
    if not tc or not tc.enabled:
        return None
    cfg = tc.config or {}
    encrypted = cfg.get("encrypted_api_key")
    if not encrypted:
        return None
    try:
        api_key = decrypt(encrypted)
    except Exception:
        return None
    if not api_key:
        return None
    return LexwareProvider(api_key=api_key)


@router.post("/angebote/extrahieren")
async def api_angebot_extrahieren(
    request: Request,
    _e=Depends(require_app_user),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Nimmt einen Freitext (Diktat oder getippt) und liefert strukturierte
    Felder fuers Angebot-Formular zurueck. Idempotent; speichert nichts.
    Body: { text: str }"""
    body = await request.json()
    text = (body.get("text") or "").strip()
    if len(text) < 5:
        return JSONResponse({"ok": False, "error": "Bitte mehr Text eingeben."}, status_code=400)
    tid = current_tenant_id(request)
    try:
        from core.ai.gemini import extract_angebot_from_text
        extracted = await extract_angebot_from_text(text, tenant_id=tid)
    except Exception as exc:  # noqa: BLE001
        logger.exception("angebot extrahieren crash: %s", exc)
        return JSONResponse({"ok": False, "error": "KI-Extraktion fehlgeschlagen."}, status_code=502)
    return JSONResponse({"ok": True, "extracted": extracted})


@router.post("/angebote/anlegen")
async def api_angebot_anlegen(
    request: Request,
    _e=Depends(require_app_inhaber),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Legt Angebot + Positionen in DB an UND erstellt ein Lexware-Draft.
    Delegiert an core.services.document_flow.create_angebot.

    Body: { kunde_name, kunde_strasse?, kunde_plz?, kunde_ort?, kunde_email?,
            intro_text?, remark_text?, positionen: [...] }
    """
    from core.services.document_flow import create_angebot
    tid = current_tenant_id(request)
    body = await request.json()
    result = await create_angebot(
        tid, kunde_name=(body.get("kunde_name") or ""),
        positionen=body.get("positionen") or [],
        kunde_strasse=body.get("kunde_strasse"), kunde_plz=body.get("kunde_plz"),
        kunde_ort=body.get("kunde_ort"), kunde_email=body.get("kunde_email"),
        intro_text=body.get("intro_text"), remark_text=body.get("remark_text"),
        quelle="web")
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@router.post("/angebote/{angebot_id}/senden")
async def api_angebot_senden(
    angebot_id: str, request: Request,
    _e=Depends(require_app_inhaber),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Verschickt Angebot per Mail an den Kunden. Delegiert an
    core.services.document_flow.send_angebot.
    Body: { to_email?: str (Default = Angebot.kunde_email), cc?: list[str] }
    """
    from core.services.document_flow import send_angebot
    tid = current_tenant_id(request)
    try:
        aid = uuid.UUID(angebot_id)
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "ungueltige id"}, status_code=400)
    body = await request.json() if (await request.body()) else {}
    result = await send_angebot(
        tid, angebot_id=aid,
        to_email=(body.get("to_email") or "").strip() or None,
        cc=body.get("cc") or None)
    if not result.get("ok"):
        code = 404 if "nicht gefunden" in (result.get("error") or "") else 400
        return JSONResponse(result, status_code=code)
    return JSONResponse({"ok": True, "message_id": result.get("message_id")})


# Angebot-Import fuer die anlegen-Route oben — wir importieren weiter unten
# um die Route am Anfang lesbar zu halten.
from core.models.angebot import Angebot  # noqa: E402  (intentional late import)


# ----- Rechnungen -----

@router.post("/rechnungen/extrahieren")
async def api_rechnung_extrahieren(
    request: Request,
    _e=Depends(require_app_user),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Freitext → KI-Extract der Rechnungs-Felder. Body: { text }"""
    body = await request.json()
    text = (body.get("text") or "").strip()
    if len(text) < 5:
        return JSONResponse({"ok": False, "error": "Bitte mehr Text eingeben."}, status_code=400)
    try:
        from core.ai.gemini import extract_rechnung_from_text
        extracted = await extract_rechnung_from_text(text)
    except Exception as exc:  # noqa: BLE001
        logger.exception("rechnung extrahieren crash: %s", exc)
        return JSONResponse({"ok": False, "error": "KI-Extraktion fehlgeschlagen."}, status_code=502)
    return JSONResponse({"ok": True, "extracted": extracted})


@router.post("/rechnungen/anlegen")
async def api_rechnung_anlegen(
    request: Request,
    _e=Depends(require_app_inhaber),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Legt eine Rechnung in der DB an UND erstellt ein Lexware-Draft.
    Delegiert an core.services.document_flow.create_rechnung.

    Body: { kunde_name, ...adresse, leistung_titel?, leistung_beschreibung?,
            betrag_brutto_eur?,  // Pauschal   ODER   positionen?: [...] }
    """
    from core.services.document_flow import create_rechnung
    tid = current_tenant_id(request)
    body = await request.json()
    result = await create_rechnung(
        tid, kunde_name=(body.get("kunde_name") or ""),
        positionen=body.get("positionen") or None,
        leistung_titel=body.get("leistung_titel"),
        leistung_beschreibung=body.get("leistung_beschreibung"),
        betrag_brutto_eur=body.get("betrag_brutto_eur"),
        kunde_strasse=body.get("kunde_strasse"), kunde_plz=body.get("kunde_plz"),
        kunde_ort=body.get("kunde_ort"), kunde_email=body.get("kunde_email"),
        input_type="web")
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@router.post("/rechnungen/{rechnung_id}/senden")
async def api_rechnung_senden(
    rechnung_id: str, request: Request,
    _e=Depends(require_app_inhaber),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Wichtig: send_rechnung_to_customer erwartet eine Lexware-Rechnung die
    NICHT mehr im Draft-Status ist (Draft = kein PDF-Download).
    Das Finalisieren passiert idealerweise im Telegram-/Cron-Flow.
    Hier rufen wir die Mail trotzdem auf — wenn Draft → kommt sauberer Fehler."""
    tid = current_tenant_id(request)
    try:
        rid = uuid.UUID(rechnung_id)
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "ungueltige id"}, status_code=400)
    body = await request.json() if (await request.body()) else {}
    to_email_override = (body.get("to_email") or "").strip() or None
    cc = body.get("cc") or None

    from core.models.rechnung import Rechnung as _Rechnung
    async with get_session() as s:
        rr = (await s.execute(
            select(_Rechnung).where(_Rechnung.id == rid).where(_Rechnung.tenant_id == tid)
        )).scalar_one_or_none()
    if rr is None:
        return JSONResponse({"ok": False, "error": "Rechnung nicht gefunden"}, status_code=404)

    to_email = to_email_override or rr.kunde_email
    if not to_email:
        return JSONResponse({"ok": False, "error": "Keine Empfaenger-Mail vorhanden."}, status_code=400)

    # Bis 2026-08-14 rief diese Route eine Funktion, die es fuer eine
    # Formular-Rechnung gar nicht gab: der Aufruf lief garantiert in einen
    # TypeError und der Nutzer bekam „Bitte erst in Lexware finalisieren".
    # Der Weg war damit strukturell tot. Jetzt macht der Versand dasselbe wie
    # beim Auftragsweg: in Lexware ausstellen, Entwurf wegraeumen, PDF holen,
    # mailen — und die Rechnung landet danach in der Bezahl-Ueberwachung.
    from core.services.document_flow import finalize_and_send_rechnung
    try:
        result = await finalize_and_send_rechnung(
            tid, rechnung_id=rid, to_email=to_email, cc=cc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("send_rechnung crash: %s", exc)
        return JSONResponse({"ok": False, "error": "Mail-Versand fehlgeschlagen."}, status_code=500)

    if not result.get("ok"):
        return JSONResponse({
            "ok": False,
            "error": result.get("error") or "Mail-Versand fehlgeschlagen.",
            "lexware_ausgestellt": result.get("lexware_ausgestellt", False),
            "queued": result.get("queued", False),
        }, status_code=502)
    return JSONResponse({
        "ok": True, "nummer": result.get("nummer"),
        "deeplink": result.get("deeplink"), "to_email": result.get("to_email"),
    })


# =================== Belege (Lexware-Voucher-Upload) ===================
# Spiegelt die Telegram-/beleg-Logik (_handle_beleg_photo_received): gleiche
# MIME-Whitelist, gleiches 10-MB-Limit, Hash-Idempotenz, gleiche
# provider.upload_voucher_file()-Logik und dasselbe Beleg-Modell.

_BELEG_ALLOWED_MIMES = {"image/jpeg", "image/png", "application/pdf"}
_BELEG_MAX_SIZE_BYTES = 10_000_000  # 10 MB (Lexware-File-Limit)
_BELEG_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "application/pdf": ".pdf"}


def _normalize_beleg_mime(raw: str | None) -> str | None:
    """Content-Type → erlaubter Beleg-MIME oder None (= nicht unterstuetzt)."""
    mime = (raw or "").split(";")[0].strip().lower()
    return mime if mime in _BELEG_ALLOWED_MIMES else None


async def _recent_belege(tenant_id: uuid.UUID, limit: int = 20) -> list[dict]:
    from core.models.beleg import (
        Beleg, BELEG_STATUS_ERROR, BELEG_STATUS_UPLOADED,
    )
    from core.integrations.lexware import LexwareProvider
    async with get_session() as s:
        rows = (await s.execute(
            select(Beleg)
            .where(Beleg.tenant_id == tenant_id)
            .order_by(Beleg.created_at.desc())
            .limit(limit)
        )).scalars().all()
    out = []
    for b in rows:
        link = (
            LexwareProvider.voucher_deeplink(b.lexware_voucher_id)
            if b.status == BELEG_STATUS_UPLOADED and b.lexware_voucher_id else None
        )
        out.append({
            "id": str(b.id),
            "zeit": _fmt_dt(b.created_at),
            "groesse_kb": (b.file_size or 0) // 1024,
            "status": b.status,
            "caption": b.caption or "",
            "lexware_link": link,
            "fehler": (b.error_message or "")[:160] if b.status == BELEG_STATUS_ERROR else "",
        })
    return out


async def _mark_beleg_error(beleg_id: uuid.UUID, msg: str) -> None:
    from core.models.beleg import Beleg, BELEG_STATUS_ERROR
    async with get_session() as s:
        b = (await s.execute(
            select(Beleg).where(Beleg.id == beleg_id)
        )).scalar_one_or_none()
        if b:
            b.status = BELEG_STATUS_ERROR
            b.error_message = msg
            await s.commit()


@router.get("/belege")
async def api_belege_list(
    request: Request, _e=Depends(require_app_user),
) -> JSONResponse:
    return JSONResponse({"belege": await _recent_belege(current_tenant_id(request))})


@router.post("/belege/{beleg_id}/vorschlag")
async def api_beleg_vorschlag(
    beleg_id: str, request: Request,
    _e=Depends(require_app_user),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Gemini liest den hochgeladenen Beleg und schlaegt die Buchung vor.

    Aendert nichts — weder bei uns noch in Lexware. Der Mensch bekommt den
    Vorschlag zu sehen und entscheidet.
    """
    from core.features.check import is_feature_enabled
    from core.services import beleg_kontierung

    tid = current_tenant_id(request)
    if not await is_feature_enabled(tid, "lexware"):
        return JSONResponse({"ok": False, "error": "Die Buchhaltung ist nicht aktiv."},
                            status_code=403)
    try:
        bid = uuid.UUID(beleg_id)
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "ungueltige id"}, status_code=400)
    return JSONResponse(await beleg_kontierung.vorschlag(tid, bid))


@router.post("/belege/{beleg_id}/kontieren")
async def api_beleg_kontieren(
    beleg_id: str, request: Request,
    _e=Depends(require_app_user),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Schreibt die bestaetigten Buchungsfelder an den Beleg in Lexware.

    Body: haendler, datum (YYYY-MM-DD), betrag_brutto_eur, mwst_prozent,
    kategorie. Die Werte kommen aus der Bestaetigungs-Karte — dort kann der
    Nutzer alles korrigieren, was Gemini falsch gelesen hat.
    """
    from core.features.check import is_feature_enabled
    from core.services import beleg_kontierung

    tid = current_tenant_id(request)
    if not await is_feature_enabled(tid, "lexware"):
        return JSONResponse({"ok": False, "error": "Die Buchhaltung ist nicht aktiv."},
                            status_code=403)
    try:
        bid = uuid.UUID(beleg_id)
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "ungueltige id"}, status_code=400)

    body = await request.json() if (await request.body()) else {}
    try:
        betrag = float(body.get("betrag_brutto_eur") or 0) or None
    except (TypeError, ValueError):
        betrag = None
    try:
        mwst = int(body.get("mwst_prozent")) if body.get("mwst_prozent") is not None else None
    except (TypeError, ValueError):
        mwst = None

    return JSONResponse(await beleg_kontierung.uebernehmen(
        tid, bid,
        haendler=(body.get("haendler") or "").strip() or None,
        datum=(body.get("datum") or "").strip() or None,
        betrag_brutto_eur=betrag,
        mwst_prozent=mwst,
        kategorie=(body.get("kategorie") or "").strip() or None,
    ))


@router.post("/belege/upload")
async def api_beleg_upload(
    request: Request,
    emp: Employee = Depends(require_app_user),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Beleg-Foto/PDF aus der PWA → Lexware-Voucher-Upload.

    Body = rohe Datei-Bytes; Content-Type bestimmt den MIME; optionale Notiz
    als ?caption=. Spiegelt exakt den Telegram-/beleg-Flow: MIME-Whitelist,
    10-MB-Limit, Hash-Idempotenz (selber Datei-Inhalt → kein Doppel-Upload),
    gleiche provider.upload_voucher_file()-Logik, gleiches Beleg-Modell.

    require_app_user (KEIN Inhaber-Gate): Belege werden oft vom Monteur im
    Feld fotografiert; verbucht wird ohnehin manuell in Lexware, hier wird
    nur abgelegt. HARTE Tenant-Isolation ueber current_tenant_id.
    """
    import hashlib
    from core.integrations.accounting_base import AccountingError
    from core.integrations.lexware import LexwareProvider
    from core.models.beleg import (
        Beleg, BELEG_SOURCE_API,
        BELEG_STATUS_UPLOADED, BELEG_STATUS_UPLOADING,
    )

    file_bytes = await request.body()
    if not file_bytes:
        return JSONResponse({"ok": False, "error": "Keine Datei empfangen."}, status_code=400)
    if len(file_bytes) > _BELEG_MAX_SIZE_BYTES:
        mb = len(file_bytes) // 1024 // 1024
        return JSONResponse(
            {"ok": False, "error": f"Datei zu gross ({mb} MB, max 10 MB)."},
            status_code=413,
        )
    mime = _normalize_beleg_mime(request.headers.get("content-type"))
    if mime is None:
        return JSONResponse(
            {"ok": False, "error": "Nur JPEG-, PNG- oder PDF-Belege erlaubt."},
            status_code=415,
        )

    caption = (request.query_params.get("caption") or "").strip()[:500] or None
    filename = (
        (request.query_params.get("filename") or "").strip()[:255]
        or f"beleg{_BELEG_EXT.get(mime, '.bin')}"
    )

    tid = current_tenant_id(request)
    provider = await _build_lexware_provider(tid)
    if provider is None:
        return JSONResponse(
            {"ok": False, "error": (
                "Lexware ist nicht verbunden. Bitte in den Einstellungen verbinden."
            )},
            status_code=409,
        )

    file_hash = hashlib.sha256(file_bytes).hexdigest()

    # Idempotenz + DB-Eintrag (Status uploading). Gleicher Datei-Inhalt schon
    # erfolgreich → sofort den vorhandenen Lexware-Link zurueckgeben.
    async with get_session() as s:
        existing = (await s.execute(
            select(Beleg).where(
                Beleg.tenant_id == tid, Beleg.file_hash == file_hash,
            )
        )).scalar_one_or_none()
        if (existing and existing.status == BELEG_STATUS_UPLOADED
                and existing.lexware_voucher_id):
            return JSONResponse({
                "ok": True, "duplikat": True, "id": str(existing.id),
                "lexware_link": LexwareProvider.voucher_deeplink(
                    existing.lexware_voucher_id
                ),
            })
        if existing:
            beleg = existing
            beleg.file_data = file_bytes
            beleg.file_mime = mime
            beleg.file_size = len(file_bytes)
            beleg.original_filename = filename
            beleg.caption = caption
            beleg.source = BELEG_SOURCE_API
            beleg.status = BELEG_STATUS_UPLOADING
            beleg.upload_attempts = (beleg.upload_attempts or 0) + 1
            beleg.error_message = None
        else:
            beleg = Beleg(
                tenant_id=tid, file_data=file_bytes, file_mime=mime,
                file_hash=file_hash, file_size=len(file_bytes),
                original_filename=filename, caption=caption,
                source=BELEG_SOURCE_API, status=BELEG_STATUS_UPLOADING,
                upload_attempts=1,
            )
        s.add(beleg)
        await s.commit()
        await s.refresh(beleg)
        beleg_id = beleg.id

    try:
        result = await provider.upload_voucher_file(
            file_bytes=file_bytes, mime_type=mime, filename=filename,
        )
    except AccountingError as e:
        await _mark_beleg_error(beleg_id, str(e)[:500])
        return JSONResponse(
            {"ok": False, "error": f"Lexware-Upload fehlgeschlagen (HTTP {e.status_code})."},
            status_code=502,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("PWA-Beleg: Lexware-Upload fehlgeschlagen: %s", e)
        await _mark_beleg_error(beleg_id, f"Unerwartet: {str(e)[:400]}")
        return JSONResponse(
            {"ok": False, "error": "Lexware-Upload fehlgeschlagen. Bitte erneut versuchen."},
            status_code=502,
        )

    async with get_session() as s:
        beleg = (await s.execute(
            select(Beleg).where(Beleg.id == beleg_id)
        )).scalar_one_or_none()
        if beleg:
            beleg.status = BELEG_STATUS_UPLOADED
            beleg.lexware_file_id = result.file_id
            beleg.lexware_voucher_id = result.voucher_id
            beleg.uploaded_at = dt.datetime.now(dt.timezone.utc)
            await s.commit()

    logger.info(
        "PWA-Beleg hochgeladen: id=%s tenant=%s mitarbeiter=%s mime=%s",
        beleg_id, tid, emp.id, mime,
    )
    deeplink = (
        LexwareProvider.voucher_deeplink(result.voucher_id)
        if result.voucher_id else None
    )
    return JSONResponse({"ok": True, "id": str(beleg_id), "lexware_link": deeplink})


@router.post("/rechnungen/pruefen")
async def api_rechnungen_pruefen(
    request: Request,
    _e=Depends(require_app_inhaber),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Gleicht den Bezahl-Status offener Rechnungen mit Lexware ab (spiegelt
    /rechnung_pruefen bzw. das Assistent-Tool rechnungen_pruefen). Nur
    Abgleich + Markierung, KEIN Versand. Inhaber, feature-gegated lexware."""
    from core.features.check import is_feature_enabled
    from core.integrations.rechnung_payment_monitor import check_pending_invoices_for_tenant
    tid = current_tenant_id(request)
    if not await is_feature_enabled(tid, "lexware"):
        return JSONResponse({"ok": False, "error": "Die Buchhaltung (Lexware) ist nicht aktiv."}, status_code=403)
    summary = await check_pending_invoices_for_tenant(tid)
    return JSONResponse({
        "ok": True,
        "geprueft": summary.get("checked", 0),
        "bezahlt": summary.get("paid", 0),
        "unveraendert": summary.get("no_change", 0),
        "fehler": summary.get("errors", 0),
    })


@router.get("/material")
async def api_material_list(
    request: Request, _e=Depends(require_app_user),
) -> JSONResponse:
    tid = current_tenant_id(request)
    from core.models.tenant_material import TenantMaterial
    async with get_session() as s:
        rows = (await s.execute(
            select(TenantMaterial)
            .where(TenantMaterial.tenant_id == tid)
            .order_by(TenantMaterial.aktiv.desc(), TenantMaterial.name)
        )).scalars().all()
    items = [
        {
            "id": str(m.id),
            "slug": m.slug,
            "name": m.name,
            "lieferant": m.lieferant_name or "",
            "bestell_link": m.bestell_link or "",
            "einheit": m.einheit,
            "standard_menge": m.standard_menge,
            "notes": m.notes or "",
            "aktiv": bool(m.aktiv),
        } for m in rows
    ]
    return JSONResponse({"items": items})


@router.post("/material/anlegen")
async def api_material_anlegen(
    request: Request,
    _e=Depends(require_app_inhaber),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Inhaber legt ein neues Material an. slug-Eindeutigkeit pro Tenant.

    Body: { name, bestell_link, lieferant?, einheit?, standard_menge?, notes? }
    """
    tid = current_tenant_id(request)
    body = await request.json()
    name = (body.get("name") or "").strip()
    bestell_link = (body.get("bestell_link") or "").strip()
    if not name or not bestell_link:
        return JSONResponse(
            {"ok": False, "error": "Name und Bestell-Link sind Pflicht."},
            status_code=400,
        )

    import re
    base_slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "material"

    from core.models.tenant_material import TenantMaterial
    async with get_session() as s:
        slug_candidate = base_slug
        i = 2
        while (await s.execute(
            select(TenantMaterial).where(TenantMaterial.tenant_id == tid).where(TenantMaterial.slug == slug_candidate)
        )).scalar_one_or_none() is not None:
            slug_candidate = f"{base_slug}-{i}"
            i += 1
            if i > 30:
                return JSONResponse({"ok": False, "error": "Slug-Konflikt."}, status_code=409)
        m = TenantMaterial(
            tenant_id=tid,
            slug=slug_candidate,
            name=name,
            bestell_link=bestell_link,
            lieferant_name=(body.get("lieferant") or "").strip() or None,
            einheit=(body.get("einheit") or "Stück").strip() or "Stück",
            standard_menge=int(body.get("standard_menge") or 1),
            notes=(body.get("notes") or "").strip() or None,
            aktiv=True,
        )
        s.add(m)
        await s.commit()
        await s.refresh(m)
    return JSONResponse({"ok": True, "id": str(m.id), "slug": m.slug})


@router.post("/material/{mid}/toggle")
async def api_material_toggle(
    mid: str, request: Request,
    _e=Depends(require_app_inhaber),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Toggelt aktiv-Flag. Material wird nicht geloescht — bleibt als
    Historie in Voice-/Telegram-Auto-Bestellungen referenzierbar."""
    tid = current_tenant_id(request)
    try:
        mid_uuid = uuid.UUID(mid)
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "ungueltige id"}, status_code=400)
    from core.models.tenant_material import TenantMaterial
    async with get_session() as s:
        m = (await s.execute(
            select(TenantMaterial)
            .where(TenantMaterial.id == mid_uuid)
            .where(TenantMaterial.tenant_id == tid)
        )).scalar_one_or_none()
        if m is None:
            return JSONResponse({"ok": False, "error": "nicht gefunden"}, status_code=404)
        m.aktiv = not m.aktiv
        await s.commit()
        new_active = m.aktiv
    return JSONResponse({"ok": True, "aktiv": new_active})


@router.post("/material/{mid}/bestellen")
async def api_material_bestellen(
    mid: str,
    request: Request,
    emp: Employee = Depends(require_app_user),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Loest eine Material-Bestellung aus: schreibt den Audit-Log-Eintrag
    (MaterialBestellung) und gibt den Bestell-Link zurueck, den die App
    oeffnet. Spiegelt den Telegram-/bestellen-Flow (_ausloesen_bestellung):
    nur Link + Log, kein Auto-Mail.

    Body (optional): { menge }. require_app_user (kein Inhaber-Gate) — der
    Monteur bestellt im Feld. HARTE Tenant-Isolation ueber current_tenant_id.
    """
    from core.models.tenant_material import (
        TenantMaterial, MaterialBestellung, BESTELL_ART_LINK,
    )

    tid = current_tenant_id(request)
    try:
        mid_uuid = uuid.UUID(mid)
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "ungueltige id"}, status_code=400)

    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        menge = int(body.get("menge") or 0)
    except (TypeError, ValueError):
        menge = 0

    async with get_session() as s:
        m = (await s.execute(
            select(TenantMaterial)
            .where(TenantMaterial.id == mid_uuid)
            .where(TenantMaterial.tenant_id == tid)
        )).scalar_one_or_none()
        if m is None:
            return JSONResponse({"ok": False, "error": "Material nicht gefunden."}, status_code=404)
        if not m.aktiv:
            return JSONResponse({"ok": False, "error": "Material ist deaktiviert."}, status_code=409)
        if menge < 1:
            menge = m.standard_menge or 1
        s.add(MaterialBestellung(
            tenant_id=tid,
            material_id=m.id,
            employee_id=emp.id,
            material_name=m.name,
            bestell_link=m.bestell_link,
            menge=menge,
            einheit=m.einheit,
            bestell_art=BESTELL_ART_LINK,
        ))
        await s.commit()
        bestell_link = m.bestell_link
        material_name = m.name
    logger.info("PWA-Bestellung: material=%s tenant=%s mitarbeiter=%s menge=%s", mid_uuid, tid, emp.id, menge)
    return JSONResponse({
        "ok": True, "bestell_link": bestell_link,
        "material": material_name, "menge": menge,
    })


@router.get("/material/bestellungen")
async def api_material_bestellungen(
    request: Request, _e=Depends(require_app_user),
) -> JSONResponse:
    """Bestellhistorie (letzte 20), tenant-gescoped. Spiegelt
    /bestellungen."""
    from core.models.tenant_material import MaterialBestellung
    tid = current_tenant_id(request)
    async with get_session() as s:
        rows = (await s.execute(
            select(MaterialBestellung)
            .where(MaterialBestellung.tenant_id == tid)
            .order_by(MaterialBestellung.created_at.desc())
            .limit(20)
        )).scalars().all()
    return JSONResponse({"bestellungen": [
        {
            "id": str(o.id),
            "material": o.material_name,
            "menge": o.menge,
            "einheit": o.einheit,
            "zeit": _fmt_dt(o.created_at),
        } for o in rows
    ]})


@router.post("/team/{slug}/abwesenheit")
async def api_team_abwesenheit(
    slug: str, request: Request,
    _e=Depends(require_app_inhaber),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Inhaber meldet einen Mitarbeiter krank / im Urlaub. Spiegel der
    Telegram-Befehle /krank + /urlaub.

    Body: { typ: 'krank'|'urlaub'|'sonstiges', start: 'YYYY-MM-DD',
            ende?: 'YYYY-MM-DD' (None = offen), notes?: str }
    """
    tid = current_tenant_id(request)
    inhaber = request.state.app_employee
    body = await request.json()
    typ = (body.get("typ") or "krank").strip()
    if typ not in ("krank", "urlaub", "sonstiges"):
        return JSONResponse(
            {"ok": False, "error": "Typ muss krank, urlaub oder sonstiges sein."},
            status_code=400,
        )
    start_iso = (body.get("start") or "").strip()
    end_iso = (body.get("ende") or "").strip() or None
    try:
        start_date = dt.date.fromisoformat(start_iso) if start_iso else dt.date.today()
    except ValueError:
        return JSONResponse({"ok": False, "error": "Start-Datum ungueltig."}, status_code=400)
    try:
        end_date = dt.date.fromisoformat(end_iso) if end_iso else None
    except ValueError:
        return JSONResponse({"ok": False, "error": "End-Datum ungueltig."}, status_code=400)

    from core.models.employee import Employee
    from core.models.employee_absence import create_absence
    async with get_session() as s:
        emp = (await s.execute(
            select(Employee)
            .where(Employee.tenant_id == tid)
            .where(Employee.slug == slug)
        )).scalar_one_or_none()
    if emp is None:
        return JSONResponse({"ok": False, "error": "Mitarbeiter nicht gefunden."}, status_code=404)

    try:
        ab = await create_absence(
            employee_id=emp.id,
            start_date=start_date,
            end_date=end_date,
            absence_type=typ,
            notes=(body.get("notes") or "").strip() or None,
            created_by_employee_id=inhaber.id,
        )
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return JSONResponse({"ok": True, "absence_id": str(ab.id)})


@router.post("/team/{slug}/zurueck")
async def api_team_zurueck(
    slug: str, request: Request,
    _e=Depends(require_app_inhaber),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Beendet die aktive Abwesenheit eines Mitarbeiters mit heutigem Datum.
    Spiegel des Telegram-Befehls /zurueck.
    """
    tid = current_tenant_id(request)
    from core.models.employee import Employee
    from core.models.employee_absence import close_absence
    async with get_session() as s:
        emp = (await s.execute(
            select(Employee)
            .where(Employee.tenant_id == tid)
            .where(Employee.slug == slug)
        )).scalar_one_or_none()
    if emp is None:
        return JSONResponse({"ok": False, "error": "Mitarbeiter nicht gefunden."}, status_code=404)
    closed = await close_absence(emp.id, dt.date.today())
    return JSONResponse({"ok": True, "had_open_absence": closed is not None})


@router.get("/einstellungen")
async def api_einstellungen_get(
    request: Request, _e=Depends(require_app_user),
) -> JSONResponse:
    """Stammdaten + verbundene Dienste. Schreiben in api_einstellungen_set."""
    tid = current_tenant_id(request)
    tenant = request.state.app_tenant
    is_inhaber = bool(request.state.app_is_inhaber)

    # Heimat-Adresse aus Tenant zusammensetzen — die App-UI zeigt eine
    # einzelne Zeile fuer einfaches Editieren.
    adresse_parts = [tenant.heimat_strasse, " · ".join(
        [p for p in [tenant.heimat_plz, tenant.heimat_ort] if p]
    )]
    adresse_join = " · ".join([p for p in adresse_parts if p])

    return JSONResponse({
        "stammdaten": {
            "company_name": tenant.company_name or "",
            "contact_name": tenant.contact_name or "",
            "contact_email": tenant.contact_email or "",
            "contact_phone": tenant.contact_phone or "",
            "branche": tenant.branche or "",
            "voice_phone_number": tenant.voice_phone_number or "",
            "heimat_strasse": tenant.heimat_strasse or "",
            "heimat_plz": tenant.heimat_plz or "",
            "heimat_ort": tenant.heimat_ort or "",
            "fahrtzeit_puffer_min": tenant.fahrtzeit_puffer_min,
            "adresse_join": adresse_join,
            "brand_color": tenant.brand_color or "",
            "website_url": tenant.website_url or "",
            "has_logo": bool(tenant.logo_data),
        },
        "features": list(getattr(tenant, "features", []) or []),
        "package_tier": tenant.package_tier or "",
        "data_retention_days": tenant.data_retention_days,
        "is_inhaber": is_inhaber,
    })


@router.post("/einstellungen")
async def api_einstellungen_set(
    request: Request,
    _e=Depends(require_app_inhaber),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Schreibt nur die Felder die fuer den Inhaber im Self-Service Sinn
    machen — OAuth/Voice-Konfig laeuft weiterhin ueber den Setup-Wizard
    bzw. das Admin-UI.

    Body: { company_name?, contact_name?, contact_email?, contact_phone?,
            heimat_strasse?, heimat_plz?, heimat_ort?, branche? }
    """
    tid = current_tenant_id(request)
    body = await request.json()
    allowed = {
        "company_name", "contact_name", "contact_email", "contact_phone",
        "heimat_strasse", "heimat_plz", "heimat_ort", "branche", "brand_color",
        "website_url",
    }
    import re as _re
    _HEX_COLOR = _re.compile(r"^#[0-9a-fA-F]{6}$")
    from core.models.tenant import Tenant
    async with get_session() as s:
        t = (await s.execute(select(Tenant).where(Tenant.id == tid))).scalar_one_or_none()
        if t is None:
            return JSONResponse({"ok": False, "error": "Tenant nicht gefunden."}, status_code=404)
        for k, v in (body or {}).items():
            if k not in allowed:
                continue
            val = (v or "").strip() if isinstance(v, str) else v
            if k == "brand_color":
                if val and not _HEX_COLOR.match(val):
                    return JSONResponse({"ok": False, "error": "Ungültige Farbe."}, status_code=400)
            if k == "website_url" and val:
                val = _normalize_website_url(val)
                if val is None:
                    return JSONResponse(
                        {"ok": False, "error": "Website muss mit http:// oder https:// beginnen."},
                        status_code=400,
                    )
            setattr(t, k, val or None)
        await s.commit()
    return JSONResponse({"ok": True})


# ─────────────────────────────────────────────────────────────────────
# Automatisierung: wie selbstaendig darf Q pro Funktion handeln?
# ─────────────────────────────────────────────────────────────────────
# Registry + Semantik der Stufen: core/features/automations.py.
# Nur der Inhaber darf schreiben — ein Geselle soll Q nicht fuer den
# ganzen Betrieb auf 'automatisch' stellen koennen.


@router.get("/automatisierung")
async def api_automatisierung_get(
    request: Request, _e=Depends(require_app_user),
) -> JSONResponse:
    """Alle fuer diesen Betrieb sichtbaren Automatisierungen mit ihrer Stufe.

    Gefiltert nach aktiven Features: was der Betrieb nicht gebucht hat,
    braucht auch keinen Schalter.
    """
    from core.features.automation_check import automation_modes_for_tenant
    from core.features.automations import (
        ALL_MODES, AUTOMATIONS, MODE_DESCRIPTIONS, MODE_LABELS,
    )
    from core.features.check import enabled_features_for_tenant

    tid = current_tenant_id(request)
    feats = await enabled_features_for_tenant(tid)
    modes = await automation_modes_for_tenant(tid)

    items = []
    for auto in AUTOMATIONS.values():
        if auto.feature and auto.feature not in feats:
            continue
        items.append({
            "key": auto.key,
            "label": auto.label,
            "description": auto.description,
            "group": auto.group,
            "mode": modes.get(auto.key, auto.default_mode),
            "allowed_modes": list(auto.allowed_modes),
            "unsupported_hint": auto.unsupported_hint,
        })

    return JSONResponse({
        "ok": True,
        "automations": items,
        "modes": [
            {"key": m, "label": MODE_LABELS[m],
             "description": MODE_DESCRIPTIONS[m]}
            for m in ALL_MODES
        ],
        "is_inhaber": bool(request.state.app_is_inhaber),
    })


@router.post("/automatisierung")
async def api_automatisierung_set(
    request: Request,
    _e=Depends(require_app_inhaber),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Setzt die Stufe einer Automatisierung.

    Body: { "key": "termin_buchen", "mode": "automatisch" }

    ``set_automation_mode`` validiert gegen die Registry (Key bekannt?
    Stufe fuer genau diese Automatisierung erlaubt?) — der Client kann
    also weder eine unbekannte Funktion anlegen noch 'assistiert' auf
    die Telefon-Buchung schummeln.
    """
    from core.features.automation_check import set_automation_mode
    from core.features.automations import AUTOMATIONS
    from core.features.check import enabled_features_for_tenant

    tid = current_tenant_id(request)
    body = await request.json()
    key = ((body or {}).get("key") or "").strip()
    mode = ((body or {}).get("mode") or "").strip()

    auto = AUTOMATIONS.get(key)
    if auto is None:
        return JSONResponse(
            {"ok": False, "error": "Unbekannte Funktion."}, status_code=400,
        )
    # Feature aus? Dann gibt es den Schalter in der UI nicht, also darf ihn
    # auch ein direkter POST nicht setzen.
    if auto.feature:
        feats = await enabled_features_for_tenant(tid)
        if auto.feature not in feats:
            return JSONResponse(
                {"ok": False, "error": "Diese Funktion ist nicht freigeschaltet."},
                status_code=403,
            )

    if not await set_automation_mode(tid, key, mode):
        return JSONResponse(
            {"ok": False,
             "error": "Diese Stufe gibt es für diese Funktion nicht."},
            status_code=400,
        )
    return JSONResponse({"ok": True, "key": key, "mode": mode})


# ─────────────────────────────────────────────────────────────────────
# Branding: Firmenlogo + Website-Link fuer die App-Kopfzeile
# ─────────────────────────────────────────────────────────────────────

# SVG ist bewusst NICHT erlaubt: eine SVG-Datei kann Skripte enthalten, und
# wir liefern das Logo von der eigenen Domain aus — das waere ein XSS-Vektor,
# den ein Tenant selbst hochladen koennte.
_LOGO_MIMES = {"image/png", "image/jpeg", "image/webp"}
_LOGO_MAX_BYTES = 512 * 1024

# Erste Bytes der erlaubten Formate. Wir glauben dem Content-Type des Clients
# nicht — sonst laedt jemand eine HTML-Datei als "image/png" hoch und wir
# liefern sie als solche wieder aus.
_LOGO_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
)


def _sniff_logo_mime(data: bytes) -> str | None:
    """Erkennt das Bildformat an den Magic Bytes. None = nicht erlaubt."""
    for magic, mime in _LOGO_MAGIC:
        if data.startswith(magic):
            return mime
    # WEBP: "RIFF" .... "WEBP"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _normalize_website_url(raw: str) -> str | None:
    """Erzwingt http/https und eine plausible Laenge. None = ungueltig.

    Ohne Schema-Zwang wuerde aus "jantos.de" im href ein relativer Link
    (/app/jantos.de), und ein "javascript:"-Schema waere ein XSS-Vektor.
    """
    url = (raw or "").strip()
    if not url:
        return None
    if not url.lower().startswith(("http://", "https://")):
        # Bequemlichkeit: reine Domain-Eingabe ("jantos.de") ergaenzen.
        if "://" in url or url.startswith(("javascript:", "data:")):
            return None
        url = "https://" + url
    if len(url) > 300 or " " in url:
        return None
    return url


@router.post("/branding/logo")
async def api_branding_logo_upload(
    request: Request,
    _e=Depends(require_app_inhaber),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Firmenlogo hochladen. Roh-Bytes im Body, Format wird an den Magic
    Bytes geprueft (nicht am Content-Type). Max 512 KB, PNG/JPEG/WEBP.
    Inhaber-only."""
    from core.models.tenant import Tenant

    data = await request.body()
    if not data:
        return JSONResponse({"ok": False, "error": "Keine Datei erhalten."}, status_code=400)
    if len(data) > _LOGO_MAX_BYTES:
        return JSONResponse(
            {"ok": False, "error": f"Logo zu groß ({len(data) // 1024} KB, max 512 KB)."},
            status_code=413,
        )
    mime = _sniff_logo_mime(data)
    if mime not in _LOGO_MIMES:
        return JSONResponse(
            {"ok": False, "error": "Nur PNG, JPEG oder WEBP."}, status_code=415,
        )

    tid = current_tenant_id(request)
    async with get_session() as s:
        t = (await s.execute(select(Tenant).where(Tenant.id == tid))).scalar_one_or_none()
        if t is None:
            return JSONResponse({"ok": False, "error": "Tenant nicht gefunden."}, status_code=404)
        t.logo_data = data
        t.logo_mime = mime
        await s.commit()
    logger.info("Logo gesetzt: tenant=%s mime=%s bytes=%s", tid, mime, len(data))
    return JSONResponse({"ok": True, "mime": mime, "bytes": len(data)})


@router.delete("/branding/logo")
async def api_branding_logo_delete(
    request: Request,
    _e=Depends(require_app_inhaber),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Logo entfernen. Inhaber-only."""
    from core.models.tenant import Tenant

    tid = current_tenant_id(request)
    async with get_session() as s:
        t = (await s.execute(select(Tenant).where(Tenant.id == tid))).scalar_one_or_none()
        if t is None:
            return JSONResponse({"ok": False, "error": "Tenant nicht gefunden."}, status_code=404)
        t.logo_data = None
        t.logo_mime = None
        await s.commit()
    return JSONResponse({"ok": True})


@router.get("/branding/logo")
async def api_branding_logo(
    request: Request, _e=Depends(require_app_user),
) -> Response:
    """Liefert das Logo des eigenen Tenants. Jeder Mitarbeiter darf es sehen
    (es steht in seiner Kopfzeile), aber nur das des eigenen Betriebs —
    die Tenant-Id kommt aus der Session, nicht aus der URL."""
    from core.models.tenant import Tenant

    tid = current_tenant_id(request)
    async with get_session() as s:
        t = (await s.execute(select(Tenant).where(Tenant.id == tid))).scalar_one_or_none()
    if t is None or not t.logo_data:
        return JSONResponse({"ok": False, "error": "Kein Logo."}, status_code=404)
    return Response(
        content=t.logo_data,
        media_type=t.logo_mime or "image/png",
        headers={
            "Cache-Control": "private, max-age=300",
            "X-Content-Type-Options": "nosniff",
        },
    )


# ─────────────────────────────────────────────────────────────────────
# Verbindungen (OAuth + API-Keys) im Einstellungen-Screen
#
# Telegram-Paritaet: der Inhaber verknuepft Google (Kalender + Drive),
# Microsoft/Outlook und Lexware Office direkt aus der App. Der OAuth-Kern
# (generate_auth_url / handle_callback / OAuthState / OAuthToken) wird
# UNVERAENDERT wiederverwendet — hier liegt nur die App-Bedienoberflaeche
# darauf. Wichtig: Google-Kalender und Drive teilen sich EINEN Google-Token
# (ein Consent deckt beide Scopes ab); Lexware ist KEIN OAuth, sondern ein
# manueller API-Key, der verschluesselt in der ToolConfig landet.
# ─────────────────────────────────────────────────────────────────────

_OAUTH_APP_PROVIDERS = ("google", "microsoft")


async def _microsoft_oauth_available() -> bool:
    """True wenn die Microsoft-OAuth-Client-Credentials hinterlegt sind
    (ToolConfig 'microsoft_oauth'). Ohne sie kann kein Tenant Outlook
    verbinden — dann blendet die App den Button aus statt einen Fehler-
    Klick anzubieten."""
    from core.models.tool_config import ToolConfig
    async with get_session() as s:
        row = (await s.execute(
            select(ToolConfig).where(ToolConfig.tool_name == "microsoft_oauth")
        )).first()
    return row is not None


async def _verbindungen_status(tid: uuid.UUID, employee_id: uuid.UUID) -> dict:
    """Liefert pro Anbieter den Verbindungs-Status fuer den Einstellungen-
    Screen. Nutzt denselben Token-Lookup (employee-aware Fallback) wie der
    Rest des Systems, damit „verbunden?" exakt das widerspiegelt, was die
    Integrationen zur Laufzeit sehen."""
    from core.security.oauth_token_lookup import find_oauth_token
    from core.integrations.google_drive import is_drive_configured
    from core.models.tool_config import ToolConfig

    # Google — ein Token deckt Kalender UND Drive (Scope-abhaengig)
    g = await find_oauth_token(tid, "google", employee_id)
    g_scopes = (getattr(g, "scopes", "") or "").split(",") if g else []
    google = {
        "connected": g is not None,
        "account": getattr(g, "account_email", None) if g else None,
        "kalender": bool(g) and any("calendar" in s for s in g_scopes),
        "drive": is_drive_configured(g),
    }

    # Microsoft / Outlook — eigener Token
    #
    # freemail: das verbundene Postfach liegt auf einer geteilten
    # Freemail-Domain (@outlook.de, @gmx.de …). Dann signiert DKIM auf die
    # Domain des Anbieters statt auf die des Betriebs — es gibt keine
    # eigene Reputation, und Erstkontakt-Mails (ein Angebot an eine neue
    # Adresse) landen ueberdurchschnittlich oft im Spam-Ordner. Und zwar
    # ohne Bounce: ohne diese Warnung wuerde es schlicht niemand merken.
    from core.utils.mail_absender import ist_freemail_adresse

    m = await find_oauth_token(tid, "microsoft", employee_id)
    m_account = getattr(m, "account_email", None) if m else None
    microsoft = {
        "connected": m is not None,
        "account": m_account,
        "available": await _microsoft_oauth_available(),
        "freemail": ist_freemail_adresse(m_account),
    }

    # Lexware — API-Key in ToolConfig (kein OAuth)
    async with get_session() as s:
        tc = (await s.execute(
            select(ToolConfig).where(
                ToolConfig.tenant_id == tid,
                ToolConfig.tool_name == "lexware",
            )
        )).scalar_one_or_none()
    lex_cfg = (tc.config or {}) if tc else {}
    lexware = {
        "connected": bool(tc and tc.enabled and lex_cfg.get("encrypted_api_key")),
        "account": lex_cfg.get("organization_id"),
    }

    return {"google": google, "microsoft": microsoft, "lexware": lexware}


@router.get("/verbindungen")
async def api_verbindungen_get(
    request: Request, _e=Depends(require_app_inhaber),
) -> JSONResponse:
    """Verbindungs-Status (Google/Microsoft/Lexware) — nur Inhaber."""
    tid = current_tenant_id(request)
    emp = request.state.app_employee
    return JSONResponse(await _verbindungen_status(tid, emp.id))


@router.post("/oauth/start")
async def api_oauth_start(
    request: Request,
    _e=Depends(require_app_inhaber),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Startet einen OAuth-Flow aus der App heraus. Liefert die Authorize-
    URL zurueck; das Frontend oeffnet sie in einem Popup (so umgeht es den
    Service-Worker und die Redirect-Kette landet sauber bei Google/MS).
    tenant_slug + employee_slug kommen aus der SESSION, nie vom Client —
    so bleibt die Tenant-Isolation hart serverseitig. Body: { provider }."""
    from core.security.oauth_flow import generate_auth_url
    body = await request.json()
    provider = (body.get("provider") or "").strip()
    if provider not in _OAUTH_APP_PROVIDERS:
        return JSONResponse({"ok": False, "error": "Unbekannter Anbieter."}, status_code=400)
    tenant = request.state.app_tenant
    emp = request.state.app_employee
    try:
        # allow_rebind=True: dieser Pfad ist per require_app_inhaber
        # authentifiziert — der Inhaber darf auch auf ein anderes Konto
        # umstellen. Der oeffentliche GET-Einstieg darf das nicht.
        auth_url = await generate_auth_url(
            tenant_slug=tenant.slug, provider=provider, employee_slug=emp.slug,
            allow_rebind=True,
        )
    except Exception:
        # Kein str(e) ans Frontend — interne Details nicht leaken.
        logger.exception("App-OAuth-Start fehlgeschlagen (provider=%s)", provider)
        return JSONResponse(
            {"ok": False,
             "error": "Verbindung konnte nicht gestartet werden. "
                      "Ist der Anbieter eingerichtet?"},
            status_code=500,
        )
    return JSONResponse({"ok": True, "auth_url": auth_url})


@router.post("/lexware/verbinden")
async def api_lexware_verbinden(
    request: Request,
    _e=Depends(require_app_inhaber),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Lexware-API-Key entgegennehmen, gegen Lexware live pruefen
    (health_check) und verschluesselt in der ToolConfig ablegen. Spiegelt
    den Telegram-/lexware_setup-Flow, ohne den Telegram-Handler zu
    importieren. Body: { api_key }."""
    from core.integrations.lexware import LexwareProvider
    from core.security.encryption import encrypt
    from core.models.tool_config import ToolConfig
    tid = current_tenant_id(request)
    body = await request.json()
    api_key = (body.get("api_key") or "").strip()
    if len(api_key) < 20 or " " in api_key:
        return JSONResponse(
            {"ok": False, "error": "Bitte einen gueltigen Lexware-API-Schluessel eingeben."},
            status_code=400,
        )
    # Live-Check, BEVOR wir speichern — kein toter Key in der DB.
    try:
        info = await LexwareProvider(api_key=api_key).health_check()
    except Exception:
        logger.warning("Lexware-Health-Check fehlgeschlagen (App-Verbindung, tenant=%s)", tid)
        return JSONResponse(
            {"ok": False, "error": "Schluessel ungueltig oder Lexware nicht erreichbar."},
            status_code=400,
        )
    org_id = (info or {}).get("organizationId")
    encrypted = encrypt(api_key)
    async with get_session() as s:
        tc = (await s.execute(
            select(ToolConfig).where(
                ToolConfig.tenant_id == tid,
                ToolConfig.tool_name == "lexware",
            )
        )).scalar_one_or_none()
        if tc is None:
            tc = ToolConfig(tenant_id=tid, tool_name="lexware", enabled=True, config={})
            s.add(tc)
        cfg = dict(tc.config or {})
        cfg["encrypted_api_key"] = encrypted
        if org_id:
            cfg["organization_id"] = org_id
        tc.config = cfg
        tc.enabled = True
        await s.commit()
    return JSONResponse({"ok": True, "account": org_id})


@router.post("/verbindungen/trennen")
async def api_verbindungen_trennen(
    request: Request,
    _e=Depends(require_app_inhaber),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Trennt eine Verbindung. Google/Microsoft: Token loeschen (der Lookup
    liefert den Tenant-Token = Default-/Inhaber-Employee). Lexware: ToolConfig
    deaktivieren + Key entfernen. Body: { provider }."""
    from core.models.tool_config import ToolConfig
    from sqlalchemy import delete as sa_delete
    tid = current_tenant_id(request)
    emp = request.state.app_employee
    body = await request.json()
    provider = (body.get("provider") or "").strip()

    if provider == "lexware":
        async with get_session() as s:
            tc = (await s.execute(
                select(ToolConfig).where(
                    ToolConfig.tenant_id == tid,
                    ToolConfig.tool_name == "lexware",
                )
            )).scalar_one_or_none()
            if tc is not None:
                cfg = dict(tc.config or {})
                cfg.pop("encrypted_api_key", None)
                tc.config = cfg
                tc.enabled = False
                await s.commit()
        return JSONResponse({"ok": True})

    if provider in _OAUTH_APP_PROVIDERS:
        from core.security.oauth_token_lookup import find_oauth_token
        from core.models import OAuthToken
        tok = await find_oauth_token(tid, provider, emp.id)
        if tok is not None:
            async with get_session() as s:
                await s.execute(sa_delete(OAuthToken).where(OAuthToken.id == tok.id))
                await s.commit()
        return JSONResponse({"ok": True})

    return JSONResponse({"ok": False, "error": "Unbekannter Anbieter."}, status_code=400)


# ─────────────────────────────────────────────────────────────────────
# Anfrage-Formular-Editor (Einstellungen → Anfrage-Formular)
#
# Telegram-Paritaet zum /formular-Wizard: der Inhaber bearbeitet die Felder
# seines oeffentlichen Anfrage-Formulars (TenantAnfrageSchema) aus der App.
# Wiederverwendung: get_schema_for_tenant / upsert_tenant_schema /
# delete_tenant_schema / validate_schema_fields aus core.integrations.
# anfrage_forms — hier liegt nur die Bedienoberflaeche + eine harte
# Namens-Normalisierung obendrauf (validate_schema_fields prueft das
# name-Format NICHT, der name landet aber als HTML-Attribut im oeffentlichen
# Formular → wir erzwingen ^[a-z][a-z0-9_]*$ und generieren bei Bedarf aus
# dem Label, damit der Inhaber sich um „technische Namen" gar nicht kuemmern
# muss).
# ─────────────────────────────────────────────────────────────────────

_ANFRAGE_FORMULAR_FEATURE = "anfrage_formular"

# Feldtypen mit Anzeige-Label (Reihenfolge wie im Telegram-Wizard)
_FIELD_TYPE_CHOICES = [
    {"value": "text", "label": "Text (eine Zeile)"},
    {"value": "textarea", "label": "Mehrzeiliger Text"},
    {"value": "tel", "label": "Telefonnummer"},
    {"value": "date", "label": "Datum"},
    {"value": "radio", "label": "Auswahl (eine Option)"},
    {"value": "checkbox_multi", "label": "Mehrfachauswahl"},
    {"value": "select", "label": "Dropdown"},
    {"value": "masse", "label": "Maße (Höhe/Breite/Tiefe)"},
    {"value": "file", "label": "Datei-Upload"},
]
_OPTION_FIELD_TYPES = {"radio", "checkbox_multi", "select"}
_ANFRAGE_TYP_CHOICES = [
    {"value": "allgemein", "label": "Allgemein"},
    {"value": "tischler", "label": "Tischler / Schreiner"},
]


# Kostenbremse fuer die Q-Zeile im Formular-Screen: je Betrieb 30 Gemini-
# Aufrufe pro Stunde. In-Memory reicht (ein Container, und das Limit ist
# eine Notbremse, keine Abrechnung).
_FORMULAR_Q_HITS: dict[str, list] = {}
_FORMULAR_Q_MAX_H = 30


def _formular_q_limit_ok(tenant_id) -> bool:
    """True wenn noch ein Q-Formular-Umbau erlaubt ist."""
    import datetime as _dt2
    now = _dt2.datetime.now(_dt2.timezone.utc)
    cutoff = now - _dt2.timedelta(hours=1)
    key = str(tenant_id)
    hits = [h for h in _FORMULAR_Q_HITS.get(key, []) if h >= cutoff]
    if len(hits) >= _FORMULAR_Q_MAX_H:
        _FORMULAR_Q_HITS[key] = hits
        logger.warning("Formular-Q: Stundenlimit erreicht (tenant=%s)", tenant_id)
        return False
    hits.append(now)
    _FORMULAR_Q_HITS[key] = hits
    if len(_FORMULAR_Q_HITS) > 2000:
        _FORMULAR_Q_HITS.clear()
    return True


def _slug_field_name(label: str, fallback: str, seen: set, reserved: set) -> str:
    """Erzeugt einen technischen Feldnamen (^[a-z][a-z0-9_]*$) aus dem Label,
    eindeutig gegen `seen` und nicht in `reserved`."""
    import re
    s = (label or fallback or "feld").lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        s = s.replace(a, b)
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    s = re.sub(r"^[0-9_]+", "", s)
    if not s:
        s = "feld"
    s = s[:28]
    base, name, i = s, s, 2
    while name in seen or name in reserved:
        name = f"{base}_{i}"
        i += 1
    return name


def _normalize_formular_fields(raw_fields: list) -> tuple[list | None, str]:
    """Saeubert die vom Client kommende Feld-Liste auf eine Whitelist von
    Keys und erzwingt saubere Feldnamen. Die fachliche Pruefung (min. 1 Feld,
    Optionen-Anzahl, Typ-Whitelist) macht danach validate_schema_fields im
    Schreibweg. Returns (fields, error_msg)."""
    import re
    from core.integrations.anfrage_forms import ALLOWED_FIELD_TYPES, RESERVED_FIELD_NAMES
    name_re = re.compile(r"^[a-z][a-z0-9_]*$")
    out: list[dict] = []
    seen: set[str] = set()
    for f in raw_fields:
        if not isinstance(f, dict):
            return None, "Ungueltiger Feld-Eintrag."
        label = (f.get("label") or "").strip()
        ftype = (f.get("type") or "").strip()
        if not label:
            return None, "Jedes Feld braucht eine Bezeichnung."
        if ftype not in ALLOWED_FIELD_TYPES:
            return None, f"Unbekannter Feldtyp '{ftype}'."
        name = (f.get("name") or "").strip().lower()
        if (not name or len(name) > 30 or not name_re.match(name)
                or name in seen or name in RESERVED_FIELD_NAMES):
            name = _slug_field_name(label, ftype, seen, RESERVED_FIELD_NAMES)
        seen.add(name)
        field: dict = {
            "name": name, "label": label[:200], "type": ftype,
            "required": bool(f.get("required")),
        }
        ph = (f.get("placeholder") or "").strip()
        if ph:
            field["placeholder"] = ph[:200]
        if ftype in _OPTION_FIELD_TYPES:
            opts = f.get("options") or []
            if isinstance(opts, str):
                opts = opts.split(",")
            opts = [str(o).strip()[:80] for o in opts if str(o).strip()][:12]
            field["options"] = opts
        out.append(field)
    return out, ""


@router.get("/formulare/{anfrage_typ}")
async def api_formular_get(
    anfrage_typ: str, request: Request, _e=Depends(require_app_inhaber),
) -> JSONResponse:
    """Aktuelles Formular-Schema (Tenant-Override oder Default) + Metadaten
    fuer den Editor. Nur Inhaber, feature-gegated.

    `anfrage_typ="auto"` liefert den Typ, den die Kunden dieses Betriebs
    tatsaechlich bekommen (Branche entscheidet). Der Editor oeffnet damit
    nie ein Formular, das gar nicht verschickt wird — das war vorher der
    Fall: er startete immer auf 'allgemein', waehrend ein Tischlerbetrieb
    seinen Kunden das Tischler-Formular schickt.
    """
    from core.integrations.anfrage_forms import (
        get_schema_for_tenant, RESERVED_FIELD_NAMES, anfrage_typ_fuer_tenant)
    from core.models.anfrage import ANFRAGE_TYP_TISCHLER, ANFRAGE_TYP_ALLGEMEIN
    from core.features.check import is_feature_enabled
    from config.settings import settings
    tid = current_tenant_id(request)
    aktiv_typ = anfrage_typ_fuer_tenant(
        getattr(request.state.app_tenant, "branche", "") or "")
    if anfrage_typ == "auto":
        anfrage_typ = aktiv_typ
    if anfrage_typ not in (ANFRAGE_TYP_TISCHLER, ANFRAGE_TYP_ALLGEMEIN):
        return JSONResponse({"ok": False, "error": "Unbekannter Formular-Typ."}, status_code=400)
    if not await is_feature_enabled(tid, _ANFRAGE_FORMULAR_FEATURE):
        return JSONResponse({"ok": False, "error": "Die Anfrage-Formular-Funktion ist nicht aktiv."}, status_code=403)
    schema = await get_schema_for_tenant(tid, anfrage_typ)
    tenant = request.state.app_tenant
    preview_url = (
        f"{settings.public_url.rstrip('/')}"
        f"/anfrage/preview/{tenant.slug}/{anfrage_typ}"
    )
    return JSONResponse({
        "ok": True,
        "anfrage_typ": anfrage_typ,
        "title": schema.get("title") or "",
        "subtitle": schema.get("subtitle") or "",
        "fields": schema.get("fields") or [],
        "field_types": _FIELD_TYPE_CHOICES,
        "option_types": sorted(_OPTION_FIELD_TYPES),
        "anfrage_typen": _ANFRAGE_TYP_CHOICES,
        "aktiv_typ": aktiv_typ,
        "reserved_names": sorted(RESERVED_FIELD_NAMES),
        "preview_url": preview_url,
    })


@router.post("/formulare/{anfrage_typ}")
async def api_formular_save(
    anfrage_typ: str, request: Request,
    _e=Depends(require_app_inhaber), _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Speichert die Formular-Felder. Body: { fields: list, title?, subtitle? }."""
    from core.integrations.anfrage_forms import upsert_tenant_schema
    from core.models.anfrage import ANFRAGE_TYP_TISCHLER, ANFRAGE_TYP_ALLGEMEIN
    from core.features.check import is_feature_enabled
    tid = current_tenant_id(request)
    if anfrage_typ not in (ANFRAGE_TYP_TISCHLER, ANFRAGE_TYP_ALLGEMEIN):
        return JSONResponse({"ok": False, "error": "Unbekannter Formular-Typ."}, status_code=400)
    if not await is_feature_enabled(tid, _ANFRAGE_FORMULAR_FEATURE):
        return JSONResponse({"ok": False, "error": "Die Anfrage-Formular-Funktion ist nicht aktiv."}, status_code=403)
    body = await request.json()
    raw_fields = body.get("fields")
    if not isinstance(raw_fields, list):
        return JSONResponse({"ok": False, "error": "Es fehlen Felder."}, status_code=400)
    fields, err = _normalize_formular_fields(raw_fields)
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    title = (body.get("title") or "").strip() or None
    subtitle = (body.get("subtitle") or "").strip() or None
    ok, msg = await upsert_tenant_schema(
        tenant_id=tid, anfrage_typ=anfrage_typ,
        fields=fields, title=title, subtitle=subtitle,
    )
    if not ok:
        return JSONResponse({"ok": False, "error": msg}, status_code=400)
    logger.info("PWA-Formular gespeichert: typ=%s tenant=%s felder=%d", anfrage_typ, tid, len(fields))
    return JSONResponse({"ok": True})


@router.post("/formulare/{anfrage_typ}/reset")
async def api_formular_reset(
    anfrage_typ: str, request: Request,
    _e=Depends(require_app_inhaber), _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Setzt das Formular auf den Branchen-Default zurueck (loescht den Tenant-
    Override) und liefert das Default-Schema zum Neu-Rendern zurueck."""
    from core.integrations.anfrage_forms import delete_tenant_schema, get_default_schema
    from core.models.anfrage import ANFRAGE_TYP_TISCHLER, ANFRAGE_TYP_ALLGEMEIN
    from core.features.check import is_feature_enabled
    tid = current_tenant_id(request)
    if anfrage_typ not in (ANFRAGE_TYP_TISCHLER, ANFRAGE_TYP_ALLGEMEIN):
        return JSONResponse({"ok": False, "error": "Unbekannter Formular-Typ."}, status_code=400)
    if not await is_feature_enabled(tid, _ANFRAGE_FORMULAR_FEATURE):
        return JSONResponse({"ok": False, "error": "Die Anfrage-Formular-Funktion ist nicht aktiv."}, status_code=403)
    await delete_tenant_schema(tid, anfrage_typ)
    schema = get_default_schema(anfrage_typ)
    logger.info("PWA-Formular zurueckgesetzt: typ=%s tenant=%s", anfrage_typ, tid)
    return JSONResponse({
        "ok": True,
        "title": schema.get("title") or "",
        "subtitle": schema.get("subtitle") or "",
        "fields": schema.get("fields") or [],
    })


@router.post("/formulare/{anfrage_typ}/vorschau")
async def api_formular_vorschau(
    anfrage_typ: str, request: Request,
    _e=Depends(require_app_inhaber), _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Rendert einen (auch ungespeicherten) Formular-Entwurf als HTML.

    Bewusst dieselbe Funktion, die der Kunde spaeter sieht
    (``render_anfrage_form_html``, preview_mode) — eine zweite Vorschau-
    Implementierung im Frontend wuerde vom Original wegdriften, und genau
    dann taeuscht sie. Body: { fields, title?, subtitle? }.

    Speichert NICHTS. Der Editor ruft das bei jeder Aenderung.
    """
    from core.integrations.anfrage_form_template import render_anfrage_form_html
    from core.models.anfrage import ANFRAGE_TYP_TISCHLER, ANFRAGE_TYP_ALLGEMEIN
    from core.features.check import is_feature_enabled
    tid = current_tenant_id(request)
    if anfrage_typ not in (ANFRAGE_TYP_TISCHLER, ANFRAGE_TYP_ALLGEMEIN):
        return JSONResponse({"ok": False, "error": "Unbekannter Formular-Typ."}, status_code=400)
    if not await is_feature_enabled(tid, _ANFRAGE_FORMULAR_FEATURE):
        return JSONResponse({"ok": False, "error": "Die Anfrage-Formular-Funktion ist nicht aktiv."}, status_code=403)
    body = await request.json()
    raw_fields = body.get("fields")
    if not isinstance(raw_fields, list):
        return JSONResponse({"ok": False, "error": "Es fehlen Felder."}, status_code=400)
    fields, err = _normalize_formular_fields(raw_fields)
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    tenant = request.state.app_tenant
    schema = {
        "title": (body.get("title") or "").strip() or "Anfrage",
        "subtitle": (body.get("subtitle") or "").strip(),
        "fields": fields,
    }
    html = render_anfrage_form_html(
        schema=schema, token="vorschau",
        company_name=tenant.company_name or "",
        branche=getattr(tenant, "branche", "") or "",
        preview_mode=True,
    )
    return JSONResponse({"ok": True, "html": html})


@router.post("/formulare/{anfrage_typ}/q")
async def api_formular_q(
    anfrage_typ: str, request: Request,
    _e=Depends(require_app_inhaber), _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Baut den Formular-Entwurf nach einer Anweisung in Alltagssprache um.

    Body: { auftrag, fields, title?, subtitle? } — `fields` ist der Stand
    im Editor (ggf. ungespeichert), damit Q auf dem arbeitet, was der
    Inhaber gerade vor sich sieht.

    Der Vorschlag wird durch dieselbe Normalisierung geschickt wie eine
    Handeingabe und dann NUR zurueckgegeben — gespeichert wird nichts.
    Uebernehmen heisst: der Inhaber sieht die Vorschau und tippt auf
    Speichern.
    """
    from core.ai.gemini import formular_umbauen
    from core.integrations.anfrage_forms import validate_schema_fields
    from core.models.anfrage import ANFRAGE_TYP_TISCHLER, ANFRAGE_TYP_ALLGEMEIN
    from core.features.check import is_feature_enabled
    tid = current_tenant_id(request)
    if anfrage_typ not in (ANFRAGE_TYP_TISCHLER, ANFRAGE_TYP_ALLGEMEIN):
        return JSONResponse({"ok": False, "error": "Unbekannter Formular-Typ."}, status_code=400)
    if not await is_feature_enabled(tid, _ANFRAGE_FORMULAR_FEATURE):
        return JSONResponse({"ok": False, "error": "Die Anfrage-Formular-Funktion ist nicht aktiv."}, status_code=403)
    body = await request.json()
    auftrag = (body.get("auftrag") or "").strip()[:1000]
    if len(auftrag) < 3:
        return JSONResponse({"ok": False, "error": "Sag mir kurz, was sich ändern soll."}, status_code=400)
    # Jeder Aufruf ist ein Gemini-Call und kostet. Der Endpunkt ist zwar nur
    # fuer den Inhaber offen, aber ein haengendes Skript (oder ein zu
    # eifriger Finger) soll das Kontingent nicht leerlaufen lassen.
    if not _formular_q_limit_ok(tid):
        return JSONResponse(
            {"ok": False, "error": "Zu viele Änderungen in kurzer Zeit — "
                                   "bitte kurz durchatmen."},
            status_code=429)
    raw_fields = body.get("fields")
    if not isinstance(raw_fields, list):
        return JSONResponse({"ok": False, "error": "Es fehlen Felder."}, status_code=400)
    ist_fields, err = _normalize_formular_fields(raw_fields)
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)

    tenant = request.state.app_tenant
    vorschlag = await formular_umbauen(
        schema={
            "title": (body.get("title") or "").strip(),
            "subtitle": (body.get("subtitle") or "").strip(),
            "fields": ist_fields,
        },
        auftrag=auftrag,
        branche=getattr(tenant, "branche", "") or "",
        company_name=tenant.company_name or "",
    )
    if not vorschlag.get("ok"):
        return JSONResponse(
            {"ok": False, "error": vorschlag.get("error") or "Q konnte das nicht umbauen."},
            status_code=502)

    # Modell-Ausgabe wie eine Nutzereingabe behandeln: erst saeubern,
    # dann fachlich pruefen. Faellt sie durch, kriegt der Inhaber eine
    # Meldung statt eines kaputten Formulars in der Vorschau.
    neu_fields, err = _normalize_formular_fields(vorschlag.get("fields") or [])
    if err:
        logger.warning("Formular-Q: Vorschlag unbrauchbar (tenant=%s): %s", tid, err)
        return JSONResponse({"ok": False, "error": f"Q-Vorschlag war unbrauchbar: {err}"}, status_code=502)
    ok, msg = validate_schema_fields(neu_fields)
    if not ok:
        logger.warning("Formular-Q: Vorschlag ungueltig (tenant=%s): %s", tid, msg)
        return JSONResponse({"ok": False, "error": f"Q-Vorschlag war ungültig: {msg}"}, status_code=502)

    logger.info("Formular-Q: Vorschlag fuer tenant=%s typ=%s felder=%d",
                tid, anfrage_typ, len(neu_fields))
    return JSONResponse({
        "ok": True,
        "title": vorschlag.get("title") or "",
        "subtitle": vorschlag.get("subtitle") or "",
        "fields": neu_fields,
        "erklaerung": vorschlag.get("erklaerung") or "",
    })


@router.post("/formulare/{anfrage_typ}/link")
async def api_formular_link_generieren(
    anfrage_typ: str,
    request: Request,
    _e=Depends(require_app_user),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Generiert einen ausfuellbaren Formular-Link fuer einen konkreten Kunden.

    Nützlich wenn der Handwerker gerade mit einem Kunden telefoniert hat
    und ihm direkt einen Token-Link per WhatsApp/SMS schicken will, ohne
    den Mail-Pipeline-Weg abzuwarten. Der Link ist 7 Tage gültig und
    verhält sich identisch zu den Mail-generierten Links.

    Body: { kunde_name, kunde_email?, kunde_telefon?, valid_days? }
    Wenn keine E-Mail bekannt: synthetischer Platzhalter (kein
    Dank-Mail-Versand nach Eingang, weil Adresse nicht real ist).
    """
    import secrets
    from core.integrations.anfrage_forms import (
        create_anfrage_token, build_anfrage_url,
    )
    from core.models.anfrage import ANFRAGE_TYP_TISCHLER, ANFRAGE_TYP_ALLGEMEIN
    from core.features.check import is_feature_enabled

    tid = current_tenant_id(request)
    if anfrage_typ not in (ANFRAGE_TYP_TISCHLER, ANFRAGE_TYP_ALLGEMEIN):
        return JSONResponse({"ok": False, "error": "Unbekannter Formular-Typ."}, status_code=400)
    if not await is_feature_enabled(tid, _ANFRAGE_FORMULAR_FEATURE):
        return JSONResponse({"ok": False, "error": "Funktion nicht aktiv."}, status_code=403)

    body = await request.json()
    kunde_name = (body.get("kunde_name") or "").strip()
    if not kunde_name:
        return JSONResponse({"ok": False, "error": "Kundenname ist Pflicht."}, status_code=400)
    kunde_email = (body.get("kunde_email") or "").strip() or None
    kunde_telefon = (body.get("kunde_telefon") or "").strip() or None
    try:
        valid_days = int(body.get("valid_days") or 7)
    except (TypeError, ValueError):
        valid_days = 7
    valid_days = max(1, min(valid_days, 30))

    # Ohne bekannte E-Mail: synthetischer Platzhalter damit das NOT-NULL-
    # Feld befüllt ist. Dank-Mail nach Eingang schlägt dann still fehl
    # (kein Brevo-Versand an .intern-Domain) — das ist gewollt.
    if not kunde_email:
        kunde_email = f"anonym-{secrets.token_hex(6)}@formular.intern"

    token_obj = await create_anfrage_token(
        tenant_id=tid,
        kunde_email=kunde_email,
        kunde_name=kunde_name,
        anfrage_typ=anfrage_typ,
        kunde_telefon=kunde_telefon,
        valid_days=valid_days,
    )
    url = build_anfrage_url(token_obj.token)
    expires_fmt = (
        token_obj.expires_at.strftime("%d.%m.%Y")
        if token_obj.expires_at else ""
    )
    logger.info(
        "PWA-Formular-Link generiert: typ=%s tenant=%s kunde=%r valid_days=%d",
        anfrage_typ, tid, kunde_name, valid_days,
    )
    return JSONResponse({
        "ok": True,
        "url": url,
        "kunde_name": kunde_name,
        "expires_at": token_obj.expires_at.isoformat() if token_obj.expires_at else None,
        "expires_fmt": expires_fmt,
    })


@router.post("/team/anlegen")
async def api_team_anlegen(
    request: Request,
    _e=Depends(require_app_inhaber),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Inhaber legt einen neuen Employee an + erzeugt einen einmaligen
    Aktivierungs-Link. Der Link wird zurueckgegeben — Inhaber kopiert
    ihn aus dem Browser und schickt ihn dem Mitarbeiter (per WhatsApp/SMS).

    Body: { name, contact_email?, job_title?, skills?: list[str] }
    """
    tid = current_tenant_id(request)
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "Name ist Pflicht."}, status_code=400)

    contact_email = (body.get("contact_email") or "").strip() or None
    job_title = (body.get("job_title") or "").strip() or None
    skills_raw = body.get("skills")
    if isinstance(skills_raw, str):
        # Bequemlichkeit fuers Frontend: komma-getrennt OK.
        skills = [s.strip() for s in skills_raw.split(",") if s.strip()]
    elif isinstance(skills_raw, list):
        skills = [str(s).strip() for s in skills_raw if str(s).strip()]
    else:
        skills = None

    # Slug = name normalisiert (Leerzeichen → "-", lowercase). Wenn schon
    # vergeben, suffix mit der id-Praefix.
    import re
    base_slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "mitarbeiter"

    from core.models.employee import Employee
    async with get_session() as s:
        # Slug-Eindeutigkeit pro Tenant: bei Konflikt zaehlt eine Zahl hoch.
        slug_candidate = base_slug
        i = 2
        while (await s.execute(
            select(Employee).where(Employee.tenant_id == tid).where(Employee.slug == slug_candidate)
        )).scalar_one_or_none() is not None:
            slug_candidate = f"{base_slug}-{i}"
            i += 1
            if i > 30:
                return JSONResponse({"ok": False, "error": "Zu viele aehnliche Slugs."}, status_code=409)

        emp = Employee(
            tenant_id=tid,
            slug=slug_candidate,
            name=name,
            contact_email=contact_email,
            job_title=job_title,
            skills=skills,
            is_default=False,
            is_active=True,
        )
        s.add(emp)
        await s.commit()
        await s.refresh(emp)
        new_emp_id = emp.id

    # Aktivierungs-Link erzeugen.
    from core.models.employee_activation_token import create_activation_token
    tok = await create_activation_token(tid, new_emp_id)
    base_url = str(request.base_url).rstrip("/")
    activation_url = f"{base_url}/app/activate?token={tok.token}"

    return JSONResponse({
        "ok": True,
        "employee_id": str(new_emp_id),
        "slug": slug_candidate,
        "activation_url": activation_url,
        "activation_short_code": tok.short_code,
        "expires_at": tok.expires_at.isoformat() if tok.expires_at else None,
    })


@router.post("/rueckrufe/anlegen")
async def api_rueckruf_anlegen(
    request: Request,
    _e=Depends(require_app_user),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Inhaber/Mitarbeiter legt manuell einen Rueckruf an (z.B. nachdem
    er telefonisch eine Bitte aufgenommen hat). Spiegel der Voice-Pipeline,
    aber ohne Audio-Quelle.

    Body: { kunde_name, kunde_telefon, anliegen, kunde_email? }
    """
    tid = current_tenant_id(request)
    employee = request.state.app_employee
    body = await request.json()
    kunde_name = (body.get("kunde_name") or "").strip()
    kunde_telefon = (body.get("kunde_telefon") or "").strip()
    anliegen = (body.get("anliegen") or "").strip()
    kunde_email = (body.get("kunde_email") or "").strip() or None

    if not kunde_name or not kunde_telefon:
        return JSONResponse(
            {"ok": False, "error": "Name und Telefon sind Pflicht."},
            status_code=400,
        )
    # Anliegen kann leer sein — wir defaulten auf einen Hinweis, damit
    # die UI-Liste nicht "leere Zeile" wird.
    if not anliegen:
        anliegen = f"Manuell angelegt von {employee.name or 'Mitarbeiter'}"

    async with get_session() as s:
        r = Rueckruf(
            tenant_id=tid,
            kunde_name=kunde_name,
            kunde_telefon=kunde_telefon,
            kunde_email=kunde_email,
            anliegen=anliegen,
            status=RUECKRUF_STATUS_OFFEN,
            assigned_employee_id=getattr(employee, "id", None),
        )
        s.add(r)
        from core.services.kunde_identity import resolve_kunde_id_safe
        r.kunde_id = await resolve_kunde_id_safe(
            s, tid, kunde_name, email=kunde_email, telefon=kunde_telefon)
        await s.commit()
        await s.refresh(r)

    return JSONResponse({"ok": True, "id": str(r.id)})


@router.get("/termine/freie-slots")
async def api_freie_slots(
    request: Request, _e=Depends(require_app_user),
) -> JSONResponse:
    """Schlaegt freie Slots fuer die kommenden N Tage vor — fuer den
    Termin-Anlage-Composer der PWA. Wrappt das Kalender-Plugin
    find_free_slots; gibt eine flache Liste {datum, uhrzeit, dauer}
    zurueck (gleiche Form wie EmailConversation.proposed_slots)."""
    tid = current_tenant_id(request)
    tenant = request.state.app_tenant
    kalender = await get_plugin_for_tenant(tenant.slug, "kalender")
    if kalender is None:
        return JSONResponse({"slots": [], "error": "Kalender nicht eingerichtet."}, status_code=200)
    try:
        days_ahead = int(request.query_params.get("days", "7"))
    except (ValueError, TypeError):
        days_ahead = 7
    try:
        out = await kalender.on_webhook("find_free_slots", {"days_ahead": days_ahead})
    except Exception as exc:  # noqa: BLE001
        logger.exception("freie-slots crash: %s", exc)
        return JSONResponse({"slots": [], "error": "Kalender-Suche fehlgeschlagen."}, status_code=200)
    return JSONResponse({"slots": out.get("slots") or []})


@router.post("/termine/anlegen")
async def api_termin_anlegen(
    request: Request,
    _e=Depends(require_app_user),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Legt einen Termin direkt im Google-Kalender an — Inhaber-Workflow.

    Body: { datum: 'DD.MM.YYYY', uhrzeit: 'HH:MM', dauer_minuten: int,
            name: str, anliegen?: str, adresse?: str, telefon?: str,
            kunde_email?: str }
    """
    tid = current_tenant_id(request)
    tenant = request.state.app_tenant
    body = await request.json()
    name = (body.get("name") or "").strip()
    datum = (body.get("datum") or "").strip()
    uhrzeit = (body.get("uhrzeit") or "").strip()
    if not name or not datum or not uhrzeit:
        return JSONResponse(
            {"ok": False, "error": "Name, Datum und Uhrzeit sind Pflichtfelder."},
            status_code=400,
        )

    kalender = await get_plugin_for_tenant(tenant.slug, "kalender")
    if kalender is None:
        return JSONResponse(
            {"ok": False, "error": "Kalender nicht eingerichtet."}, status_code=400,
        )

    payload = {
        "name": name,
        "datum": datum,
        "uhrzeit": uhrzeit,
        "dauer_minuten": int(body.get("dauer_minuten") or 60),
        "anliegen": (body.get("anliegen") or "").strip() or None,
        "adresse": (body.get("adresse") or "").strip() or None,
        "telefon": (body.get("telefon") or "").strip() or None,
        "kunde_email": (body.get("kunde_email") or "").strip() or None,
    }
    try:
        res = await kalender.on_webhook("book_appointment", payload)
    except Exception as exc:  # noqa: BLE001
        logger.exception("termin anlegen crash: %s", exc)
        return JSONResponse({"ok": False, "error": "Buchung fehlgeschlagen."}, status_code=500)

    # book_appointment liefert je nach Plugin-Stand verschiedene Form-Strings;
    # gewohnte Felder: event_id oder erfolg=True
    if res.get("error"):
        return JSONResponse({"ok": False, "error": res.get("error")}, status_code=409)
    return JSONResponse({
        "ok": True,
        "event_id": res.get("event_id"),
        "datum": payload["datum"],
        "uhrzeit": payload["uhrzeit"],
    })


@router.post("/anfragen/{anfrage_id}/reply")
async def api_anfrage_reply(
    anfrage_id: str, request: Request,
    _e=Depends(require_app_user),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Beantwortet eine Anfrage per Mail (RFC-gethreaded). Delegiert an
    core.services.document_flow.send_anfrage_reply.
    Body: { "body": "Antwort-Text", "close": true|false }
    """
    from core.services.document_flow import send_anfrage_reply
    tid = current_tenant_id(request)
    employee = request.state.app_employee
    try:
        cid = uuid.UUID(anfrage_id)
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "ungueltige id"}, status_code=400)
    body_data = await request.json()
    reply_text = ((body_data or {}).get("body") or "").strip()
    if not reply_text:
        return JSONResponse({"ok": False, "error": "Leere Antwort."}, status_code=400)
    close_after = bool((body_data or {}).get("close", False))
    result = await send_anfrage_reply(
        tid, conv_id=cid, reply_text=reply_text,
        employee_id=getattr(employee, "id", None), close=close_after)
    if not result.get("ok"):
        err = result.get("error") or ""
        code = 404 if "nicht gefunden" in err else (502 if "Versand" in err else 400)
        return JSONResponse(result, status_code=code)
    return JSONResponse({
        "ok": True,
        "internet_message_id": result.get("internet_message_id"),
        "closed": result.get("closed")})
# =====================================================================
# Assistent — Gemini-Kommando-Zentrale
# =====================================================================
#
# Der Handwerker tippt oder spricht einen Befehl; Gemini entscheidet per
# Function-Calling, welches Tool auszufuehren ist (siehe
# core/ai/command_center.py). Read-Tools laufen sofort, Write-Tools werden
# erst nach Bestaetigung ausgefuehrt — daher zwei Endpunkte.

# Obergrenze fuer den Freigabe-Request (Mail-Anhaenge reisen als Base64 mit).
_ASSISTENT_MAX_BODY_BYTES = 20 * 1024 * 1024


async def _build_command_ctx(request: Request):
    """Baut den Ausfuehrungskontext (tenant-isoliert) fuer das command_center."""
    from core.ai.command_center import Ctx
    from core.features.automation_check import automation_modes_for_tenant
    from core.features.check import enabled_features_for_tenant

    tid = current_tenant_id(request)
    feats = await enabled_features_for_tenant(tid)
    modes = await automation_modes_for_tenant(tid)
    return Ctx(
        tenant=request.state.app_tenant,
        employee=request.state.app_employee,
        tid=tid,
        features=set(feats),
        automation_modes=modes,
    )


@router.post("/assistent")
async def api_assistent(
    request: Request,
    _e=Depends(require_app_user),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Nimmt einen natuersprachlichen Befehl entgegen und laesst Gemini das
    passende Tool waehlen.

    Body: { "text": "..." }
    Antwort (eines von):
      { "type": "message", "text": ... }                 — Antwort/Rueckfrage
      { "type": "confirm", "tool", "args", "summary", "frage" } — Bestaetigung noetig
      { "type": "error",  "text": ... }
    """
    from core.ai.command_center import run_command

    body = await request.json()
    text = ((body or {}).get("text") or "").strip()
    if not text:
        return JSONResponse({"type": "error", "text": "Bitte einen Befehl eingeben."}, status_code=400)
    if len(text) > 1000:
        text = text[:1000]

    # Gesprächsverlauf (frühere Turns) für Mehrfach-Rückfragen — der Client
    # schickt eine Liste {role: "user"|"model", text: str}. Defensiv begrenzt.
    history_raw = (body or {}).get("history")
    history = []
    if isinstance(history_raw, list):
        for turn in history_raw[-12:]:
            if not isinstance(turn, dict):
                continue
            role = "model" if turn.get("role") == "model" else "user"
            t = (turn.get("text") or "").strip()
            if t:
                history.append({"role": role, "text": t[:1000]})

    # Aktueller Screen-Kontext (welche Ansicht / welcher Kunde ist gerade offen)
    screen_context: dict | None = None
    sc_raw = (body or {}).get("screen_context")
    if isinstance(sc_raw, dict):
        screen  = (sc_raw.get("screen")   or "").strip()[:100]
        kunde   = (sc_raw.get("kunde")    or "").strip()[:200]
        notizen = (sc_raw.get("notizen")  or "").strip()[:2000]
        briefing = (sc_raw.get("briefing") or "").strip()[:500]
        if screen:
            screen_context = {"screen": screen, "kunde": kunde or None,
                              "notizen": notizen or None, "briefing": briefing or None}

    ctx = await _build_command_ctx(request)
    result = await run_command(text, ctx, history=history, screen_context=screen_context)
    from core.models.app_usage_event import record_app_usage, USAGE_ASSISTENT_BEFEHL
    await record_app_usage(ctx.tid, getattr(ctx.employee, "id", None), USAGE_ASSISTENT_BEFEHL)
    return JSONResponse(result)


@router.post("/assistent/ausfuehren")
async def api_assistent_ausfuehren(
    request: Request,
    _e=Depends(require_app_user),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Fuehrt eine zuvor vom /assistent vorgeschlagene Write-Aktion aus —
    NACH ausdruecklicher Bestaetigung des Nutzers.

    Body: { "tool": "...", "args": {...} }

    Der Body ist normalerweise winzig; nur der Mail-Entwurf schickt
    Anhaenge als Base64 mit. Darum eine grosszuegige, aber harte Obergrenze
    (der Versand selbst begrenzt danach nochmal pro Anhang).
    """
    from core.ai.command_center import execute_confirmed

    try:
        laenge = int(request.headers.get("content-length") or 0)
    except ValueError:
        laenge = 0
    if laenge > _ASSISTENT_MAX_BODY_BYTES:
        return JSONResponse(
            {"type": "error", "text": "Die Anhaenge sind zu gross (max 20 MB)."},
            status_code=413)

    body = await request.json()
    tool = ((body or {}).get("tool") or "").strip()
    args = (body or {}).get("args") or {}
    if not tool:
        return JSONResponse({"type": "error", "text": "Keine Aktion angegeben."}, status_code=400)
    if not isinstance(args, dict):
        return JSONResponse({"type": "error", "text": "Ungueltige Argumente."}, status_code=400)

    ctx = await _build_command_ctx(request)
    result = await execute_confirmed(tool, args, ctx)
    if result.get("type") == "done":
        from core.models.app_usage_event import record_app_usage, USAGE_ASSISTENT_AKTION
        await record_app_usage(ctx.tid, getattr(ctx.employee, "id", None), USAGE_ASSISTENT_AKTION)
    status = 200 if result.get("type") == "done" else 400
    return JSONResponse(result, status_code=status)


_OBJEKT_MAX_BYTES = 15 * 1024 * 1024
_OBJEKT_MIMES = ("image/jpeg", "image/png", "image/webp")


@router.post("/objekt/frage")
async def api_objekt_frage(
    request: Request,
    _e=Depends(require_app_user),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Foto eines Geraets/Bauteils → was ist das, Antwort aus der Doku.

    Body: rohe Bild-Bytes, Content-Type = MIME.
    Query: ``?frage=`` (was der Handwerker wissen will),
           ``?modus=kaufen`` fuer Bezugsquellen statt Doku,
           ``?hist=`` (JSON [{role,text}]) fuer Rueckfragen zum selben Bild.

    Gemini bekommt hier die Google-Suche als Werkzeug (Grounding, Vertex
    europe-west3). Das Bild bleibt in Frankfurt; nach draussen gehen nur die
    Suchbegriffe, die das Modell daraus bildet — die stehen als
    ``suchanfragen`` in der Antwort, damit nachvollziehbar ist, was gesucht
    wurde.
    """
    import json

    from core.ai.gemini import objekt_erkennen, objekt_kaufen
    from core.features.check import is_feature_enabled
    from core.models.tenant import Tenant

    tid = current_tenant_id(request)
    if not await is_feature_enabled(tid, "objekt_suche"):
        return JSONResponse(
            {"ok": False, "error": "Objekt-Erkennung ist für diesen Betrieb nicht aktiv."},
            status_code=403)

    image_bytes = await request.body()
    if not image_bytes or len(image_bytes) < 100:
        return JSONResponse({"ok": False, "error": "Kein Bild empfangen."}, status_code=400)
    if len(image_bytes) > _OBJEKT_MAX_BYTES:
        return JSONResponse({"ok": False, "error": "Bild zu groß (max 15 MB)."},
                            status_code=413)
    mime = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if mime not in _OBJEKT_MIMES:
        return JSONResponse(
            {"ok": False, "error": "Nur Bilder (JPEG, PNG, WebP) werden unterstützt."},
            status_code=415)

    frage = (request.query_params.get("frage") or "")[:1000]
    modus = (request.query_params.get("modus") or "").strip()
    verlauf = []
    roh = request.query_params.get("hist")
    if roh:
        try:
            geladen = json.loads(roh)
            if isinstance(geladen, list):
                verlauf = geladen[-8:]
        except (ValueError, TypeError):
            verlauf = []

    async with get_session() as s:
        branche = (await s.execute(
            select(Tenant.branche).where(Tenant.id == tid))).scalar_one_or_none()

    if modus == "kaufen":
        ergebnis = await objekt_kaufen(
            image_bytes, mime, frage, tenant_id=str(tid), verlauf=verlauf)
    else:
        ergebnis = await objekt_erkennen(
            image_bytes, mime, frage, tenant_id=str(tid),
            branche=branche, verlauf=verlauf)
    ergebnis["modus"] = modus or "doku"
    return JSONResponse(ergebnis)


@router.post("/objekt/merken")
async def api_objekt_merken(
    request: Request,
    emp: Employee = Depends(require_app_user),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Ein gefundenes Teil als Material mit Bestell-Link ablegen.

    Damit landet der Fund nicht in einem Chatverlauf, sondern im
    Material-Katalog, den der Betrieb ohnehin fuer Nachbestellungen nutzt —
    beim naechsten Mal ist es ein Knopfdruck statt einer neuen Suche.

    Body: { name, bestell_link, lieferant?, notiz? }
    """
    from core.models.tenant_material import TenantMaterial

    tid = current_tenant_id(request)
    body = await request.json() if (await request.body()) else {}
    name = (body.get("name") or "").strip()[:200]
    link = (body.get("bestell_link") or "").strip()[:2000]
    if len(name) < 2:
        return JSONResponse({"ok": False, "error": "Bitte einen Namen angeben."},
                            status_code=400)
    if not link.startswith(("http://", "https://")):
        return JSONResponse({"ok": False, "error": "Bitte einen gültigen Link angeben."},
                            status_code=400)

    basis = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "teil"
    async with get_session() as s:
        vorhanden = set((await s.execute(
            select(TenantMaterial.slug).where(TenantMaterial.tenant_id == tid)
        )).scalars().all())
        slug = basis
        n = 2
        while slug in vorhanden:
            slug = f"{basis}-{n}"[:80]
            n += 1
        material = TenantMaterial(
            tenant_id=tid, slug=slug, name=name, bestell_link=link,
            lieferant_name=(body.get("lieferant") or "").strip()[:200] or None,
            notes=(body.get("notiz") or "").strip()[:1000] or None,
        )
        s.add(material)
        await s.commit()
        await s.refresh(material)
        mid = material.id

    logger.info("Objekt als Material gemerkt: %s (tenant=%s)", slug, tid)
    return JSONResponse({"ok": True, "id": str(mid), "slug": slug, "name": name})


@router.post("/assistent/mit-bild")
async def api_assistent_mit_bild(
    request: Request,
    _e=Depends(require_app_user),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Bild + (optionaler) Freitext an Q. Q entscheidet per Function-Calling,
    was mit dem Bild geschehen soll — visualisieren, im Kundenarchiv ablegen
    oder als Beleg erfassen — oder fragt nach bzw. beantwortet eine Frage zum
    Bild. Die eigentliche Aktion fuehrt das Frontend danach mit den
    vorhandenen Endpunkten aus (es haelt die Bytes ohnehin).

    Body: rohe Bild-Bytes. Content-Type = MIME (image/jpeg|png|webp).
    Query: ?text=... (URL-kodiert, max 1000 Zeichen),
           ?hist=... (URL-kodiertes JSON [{role,text}], fuer Rueckfragen)

    Antwort (eines von):
      { "type": "message", "text": ... }                       — Antwort/Rueckfrage
      { "type": "action", "action": "visualisieren", "beschreibung": ... }
      { "type": "action", "action": "archiv", "kunde_name": ... }
      { "type": "action", "action": "beleg" }
    """
    import json

    from core.ai.gemini import route_image_intent

    image_bytes = await request.body()
    if not image_bytes or len(image_bytes) < 100:
        return JSONResponse({"type": "error", "text": "Kein Bild empfangen."}, status_code=400)
    if len(image_bytes) > 15 * 1024 * 1024:
        return JSONResponse(
            {"type": "error", "text": "Bild zu groß (max 15 MB)."},
            status_code=413,
        )

    mime = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if mime not in ("image/jpeg", "image/png", "image/webp"):
        return JSONResponse(
            {"type": "error", "text": "Nur Bilder (JPEG, PNG, WebP) werden unterstützt."},
            status_code=415,
        )

    text = (request.query_params.get("text") or "").strip()[:1000]

    # Kurzer Verlauf der Bild-Konversation (fuer Rueckfragen wie „welcher
    # Kunde?" → „Müller"). Defensiv geparst und begrenzt.
    history = []
    raw_hist = request.query_params.get("hist") or ""
    if raw_hist:
        try:
            parsed = json.loads(raw_hist)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, list):
            for turn in parsed[-8:]:
                if not isinstance(turn, dict):
                    continue
                role = "model" if turn.get("role") == "model" else "user"
                t = (turn.get("text") or "").strip()
                if t:
                    history.append({"role": role, "text": t[:500]})

    ctx = await _build_command_ctx(request)
    company_name = getattr(ctx.tenant, "company_name", "") or "dem Betrieb"
    raw_name = getattr(ctx.employee, "name", "") or ""
    employee_name = raw_name.split()[0] if raw_name.strip() else "dir"

    try:
        result = await route_image_intent(
            image_bytes,
            mime_type=mime,
            text=text,
            features=ctx.features,
            company_name=company_name,
            employee_name=employee_name,
            history=history,
        )
    except Exception as exc:
        logger.exception("api_assistent_mit_bild: %s", exc)
        return JSONResponse(
            {"type": "error", "text": "Fehler beim Verarbeiten des Bildes."},
            status_code=500,
        )

    from core.models.app_usage_event import record_app_usage, USAGE_ASSISTENT_BEFEHL
    await record_app_usage(ctx.tid, getattr(ctx.employee, "id", None), USAGE_ASSISTENT_BEFEHL)
    return JSONResponse(result)


@router.post("/assistent/transkript")
async def api_assistent_transkript(
    request: Request,
    _e=Depends(require_app_user),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Sprachbefehl → Text. Der Browser nimmt auf (WAV 16 kHz, wie beim
    Diktat) und schickt die rohen Bytes; wir transkribieren WORTGETREU
    (kein Schema, keine DB-Speicherung) und geben den Text zurueck, den
    die App dann ins Befehlsfeld setzt.

    Body: rohe Audio-Bytes. Content-Type = MIME. Antwort: { "text": ... }
    """
    from core.ai.gemini import transcribe_audio

    audio_bytes = await request.body()
    err = _validate_diktat_audio(audio_bytes)
    if err:
        return JSONResponse({"ok": False, "error": err[0]}, status_code=err[1])
    mime = _normalize_diktat_mime(request.headers.get("content-type"))
    if mime is None:
        return JSONResponse(
            {"ok": False, "error": "Audioformat wird nicht unterstuetzt."},
            status_code=415,
        )
    try:
        text = await transcribe_audio(audio_bytes, mime_type=mime)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Assistent-Transkript fehlgeschlagen: %s", exc)
        return JSONResponse(
            {"ok": False, "error": "Konnte nicht transkribieren. Bitte erneut."},
            status_code=502,
        )
    return JSONResponse({"ok": True, "text": (text or "").strip()})


# =================== Welle 7: Diagnose + Verbindungs-Tests ===================
#
# Spiegelt die Telegram-Diagnose-Befehle (/status, /microsoft_check,
# /lexware_status, /kalender_status, /werkstatt_status). Beantwortet
# fuer den Inhaber/Mitarbeiter die Frage "laeuft das Tool gerade?" ohne
# dass er ueber den Server-Status-Page muss.


@router.get("/diagnose")
async def api_diagnose(
    request: Request, _e=Depends(require_app_user),
) -> JSONResponse:
    """Aggregierter Health-Report fuer die App: Verbindungen, Cron-
    Heartbeats, Mail-Pipeline-Stand, Werkstatt-Adresse.

    Sichtbar fuer alle Rollen — der Inhaber will wissen "geht meine
    Investition", der Mitarbeiter will wissen "warum kommen meine Mails
    gerade nicht durch".
    """
    tid = current_tenant_id(request)
    emp = request.state.app_employee

    # 1) Verbindungs-Status — Reuse _verbindungen_status (Inhaber-Schutz
    # entfaellt hier: alle Rollen sehen das Verbindungs-Tableau, aber
    # weder Email-Adresse-Detail noch Lexware-Org-ID sind sensibel genug
    # um die Sichtbarkeit zu begrenzen).
    raw = await _verbindungen_status(tid, emp.id)
    verbindungen = [
        {
            "dienst": "microsoft", "label": "Outlook / Mail",
            "status": "ok" if raw["microsoft"]["connected"] else "danger",
            "detail": raw["microsoft"]["account"] or (
                "nicht eingerichtet" if not raw["microsoft"]["available"]
                else "nicht verbunden"
            ),
        },
        {
            "dienst": "kalender", "label": "Google Kalender",
            "status": "ok" if raw["google"]["kalender"] else (
                "warn" if raw["google"]["connected"] else "danger"
            ),
            "detail": raw["google"]["account"] or "nicht verbunden",
        },
        {
            "dienst": "drive", "label": "Google Drive (Kunden-Archiv)",
            "status": "ok" if raw["google"]["drive"] else (
                "warn" if raw["google"]["connected"] else "danger"
            ),
            "detail": raw["google"]["account"] or "nicht verbunden",
        },
        {
            "dienst": "lexware", "label": "Lexware (Buchhaltung)",
            "status": "ok" if raw["lexware"]["connected"] else "danger",
            "detail": raw["lexware"]["account"] or "nicht verbunden",
        },
    ]

    # 2) Cron-Heartbeats — process-globaler Report, nicht tenant-spezifisch.
    # Wir zeigen ihn trotzdem an, weil die Crons den Tenant indirekt
    # bedienen (Mail-Polling, DSGVO-Cleanup, etc.).
    from core.integrations.cron_health import get_health_report
    health = get_health_report()
    crons = []
    for name, info in (health.get("crons") or {}).items():
        crons.append({
            "name": name,
            "ok": bool(info.get("alive")),
            "age_min": info.get("minutes_since"),
            "max_min": info.get("max_minutes"),
        })

    # 3) Mail-Pipeline-Stand
    from core.models.failed_mail_queue import FailedMailQueue
    async with get_session() as s:
        last_mail = (await s.execute(
            select(func.max(EmailConversation.updated_at))
            .where(EmailConversation.tenant_id == tid)
        )).scalar_one_or_none()
        failed_count = (await s.execute(
            select(func.count(FailedMailQueue.id))
            .where(FailedMailQueue.tenant_id == tid)
        )).scalar_one() or 0
    mail_info = {
        "last_eingang": last_mail.isoformat() if last_mail else None,
        "last_eingang_fmt": _fmt_dt(last_mail) if last_mail else "",
        "failed_queue": int(failed_count),
    }

    # 4) Werkstatt-Adresse — spiegelt /werkstatt_status. Aus dem Default-
    # Employee weil dort die Quelle der Wahrheit ist (Telegram-Code
    # spiegelt den Wert anschliessend auf tenant.heimat_*, aber Employee
    # ist die kanonische Stelle).
    async with get_session() as s:
        default_emp = (await s.execute(
            select(Employee).where(Employee.tenant_id == tid)
            .where(Employee.is_default == True)  # noqa: E712
            .limit(1)
        )).scalar_one_or_none()
    if default_emp is not None:
        werkstatt = {
            "strasse": default_emp.heimat_strasse or "",
            "plz": default_emp.heimat_plz or "",
            "ort": default_emp.heimat_ort or "",
            "gesetzt": bool(default_emp.heimat_strasse and default_emp.heimat_ort),
        }
    else:
        werkstatt = {"strasse": "", "plz": "", "ort": "", "gesetzt": False}

    return JSONResponse({
        "verbindungen": verbindungen,
        "crons": crons,
        "mail": mail_info,
        "werkstatt": werkstatt,
    })


@router.post("/verbindungen/{dienst}/test")
async def api_verbindung_test(
    dienst: str, request: Request,
    _e=Depends(require_app_inhaber),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Live-Test einer eingerichteten Verbindung. Inhaber-only weil
    der Test echte API-Aufrufe macht (Test-Mail kostet ggf. Kontingent).

    dienst ∈ {microsoft, kalender, lexware, drive}
    """
    tid = current_tenant_id(request)
    tenant = request.state.app_tenant
    emp = request.state.app_employee

    if dienst == "microsoft":
        # Test-Mail an die im Employee-Profil hinterlegte Kontakt-Adresse.
        # Faellt zurueck auf contact_email vom Tenant, falls Employee keine hat.
        to_email = (
            getattr(emp, "contact_email", None)
            or getattr(tenant, "contact_email", None)
        )
        if not to_email:
            return JSONResponse(
                {"ok": False,
                 "error": "Keine Empfaenger-Mail im Profil. Bitte in Einstellungen ergaenzen."},
                status_code=400,
            )
        try:
            from core.integrations.microsoft import send_mail_as_user
            ok = await send_mail_as_user(
                tenant_id=tid,
                to_email=to_email,
                subject="Gewerbeagent Test-Mail",
                body_html=(
                    "<p>Hallo,</p>"
                    "<p>diese Test-Mail bestaetigt, dass dein verbundenes "
                    "Microsoft-/Outlook-Konto in Gewerbeagent funktioniert.</p>"
                    "<p>Du kannst diese Mail jetzt loeschen.</p>"
                ),
                body_text=(
                    "Gewerbeagent Test-Mail — dein Microsoft-/Outlook-Konto ist verbunden."
                ),
                employee_id=getattr(emp, "id", None),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("microsoft test crash: %s", exc)
            return JSONResponse({"ok": False, "error": "Test fehlgeschlagen."}, status_code=502)
        if not ok:
            return JSONResponse({"ok": False, "error": "Microsoft Graph hat den Versand abgelehnt."}, status_code=502)
        return JSONResponse({"ok": True, "detail": f"Test-Mail an {to_email} gesendet."})

    if dienst == "kalender":
        kalender = await get_plugin_for_tenant(tenant.slug, "kalender")
        if kalender is None:
            return JSONResponse({"ok": False, "error": "Kalender-Plugin nicht eingerichtet."}, status_code=400)
        try:
            out = await kalender.on_webhook("find_free_slots", {"days_ahead": 3})
        except Exception as exc:  # noqa: BLE001
            logger.exception("kalender test crash: %s", exc)
            return JSONResponse({"ok": False, "error": "Kalender-Aufruf gescheitert."}, status_code=502)
        slots = out.get("slots") or []
        return JSONResponse({
            "ok": True,
            "detail": f"{len(slots)} freie Slot(s) in den naechsten 3 Tagen gefunden.",
        })

    if dienst == "lexware":
        provider = await _build_lexware_provider(tid)
        if provider is None:
            return JSONResponse({"ok": False, "error": "Lexware nicht verbunden."}, status_code=400)
        try:
            info = await provider.health_check()
        except Exception as exc:  # noqa: BLE001
            logger.exception("lexware test crash: %s", exc)
            return JSONResponse({"ok": False, "error": f"Lexware-API antwortet nicht: {str(exc)[:200]}"}, status_code=502)
        org = (info or {}).get("organizationId") or (info or {}).get("companyName") or "OK"
        return JSONResponse({"ok": True, "detail": f"Lexware antwortet — {org}"})

    if dienst == "drive":
        from core.integrations.google_drive import (
            get_drive_service, list_tenant_kunde_drives,
        )
        from core.security.oauth_token_lookup import find_oauth_token
        token = await find_oauth_token(tid, "google", emp.id)
        if token is None:
            return JSONResponse({"ok": False, "error": "Google nicht verbunden."}, status_code=400)
        try:
            # Erst Service holen (testet Token-Refresh-Pfad), dann eine
            # leichtgewichtige Listing-Funktion aufrufen.
            await get_drive_service(token)
            kunden_dirs = await list_tenant_kunde_drives(tenant)
        except Exception as exc:  # noqa: BLE001
            logger.exception("drive test crash: %s", exc)
            return JSONResponse({"ok": False, "error": f"Drive-API: {str(exc)[:200]}"}, status_code=502)
        return JSONResponse({
            "ok": True,
            "detail": f"Drive-Verbindung ok — {len(kunden_dirs)} Kunden-Ordner gefunden.",
        })

    return JSONResponse({"ok": False, "error": f"Unbekannter Dienst '{dienst}'."}, status_code=400)
