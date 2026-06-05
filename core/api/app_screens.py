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

import datetime as dt
import logging
import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
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
    KUNDENGESPRAECH_STATUS_ANGENOMMEN,
    KUNDENGESPRAECH_STATUS_ERFASST,
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


@router.get("/angebote")
async def api_angebote(request: Request, _e=Depends(require_app_user)) -> JSONResponse:
    tid = current_tenant_id(request)
    async with get_session() as s:
        rows = (await s.execute(
            select(Angebot)
            .where(Angebot.tenant_id == tid)
            .order_by(Angebot.created_at.desc())
            .limit(50)
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
        })
    return JSONResponse({"angebote": out})


@router.get("/rechnungen")
async def api_rechnungen(request: Request, _e=Depends(require_app_user)) -> JSONResponse:
    tid = current_tenant_id(request)
    async with get_session() as s:
        rows = (await s.execute(
            select(Rechnung)
            .where(Rechnung.tenant_id == tid)
            .order_by(Rechnung.created_at.desc())
            .limit(50)
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
        })
    return JSONResponse({"rechnungen": out})


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
    """Laufende Auftraege = Angebote, deren Status im Lifecycle liegt
    (inkl. abgebrochen). Tenant-gescoped."""
    tid = current_tenant_id(request)
    relevante = set(AUFTRAG_LIFECYCLE) | {ANGEBOT_STATUS_ABGEBROCHEN}
    async with get_session() as s:
        rows = (await s.execute(
            select(Angebot)
            .where(Angebot.tenant_id == tid, Angebot.status.in_(relevante))
            .order_by(Angebot.created_at.desc())
            .limit(50)
        )).scalars().all()
    out = []
    for a in rows:
        out.append({
            "id": str(a.id),
            "kunde": a.kunde_name,
            "betrag": _fmt_eur(a.gesamtbetrag_brutto_eur),
            "status": a.status,
            "status_label": AUFTRAG_LIFECYCLE_LABELS.get(a.status, a.status),
            "schritt": AUFTRAG_LIFECYCLE.index(a.status) if a.status in AUFTRAG_LIFECYCLE else None,
            "schritte_gesamt": len(AUFTRAG_LIFECYCLE),
            "abgebrochen": a.status == ANGEBOT_STATUS_ABGEBROCHEN,
            "zeit": _fmt_dt(a.created_at),
        })
    return JSONResponse({"auftraege": out})


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
    return out


@router.get("/aktuelles")
async def api_aktuelles(request: Request, _e=Depends(require_app_user)) -> JSONResponse:
    """Aggregiert alles Relevante für den Start-Screen 'Aktuelles'."""
    tid = current_tenant_id(request)
    return JSONResponse({
        "rueckrufe": await _open_rueckrufe(tid),
        "beratung": await _beratung_leads(tid),
        "auftraege": await _aktuelle_auftraege(tid),
    })


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
) -> uuid.UUID:
    """Speichert ein Kundengespraech aus der Diktat-Extraktion.

    Spiegelt exakt das Mapping aus dem Telegram-Flow
    (_handle_aufnahme_audio_received); zusaetzlich wird der diktierende
    Mitarbeiter als created_by/assigned vermerkt.
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
        await s.commit()
        return g.id


@router.post("/aufnahmen/diktat")
async def api_aufnahme_diktat(
    request: Request,
    emp: Employee = Depends(require_app_user),
    _c=Depends(require_app_csrf),
) -> JSONResponse:
    """Sprach-Diktat aus dem Browser → Gemini-Analyse → Kundengespraech.

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

    g_id = await _save_diktat_gespraech(tid, emp.id, kunde_name, dauer, extracted)
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

    async with get_session() as s:
        viz = Visualisierung(
            tenant_id=tid, original_image_data=image_bytes,
            prompt=prompt, status=VIZ_STATUS_GENERATING,
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
    logger.info("PWA-Visualisierung fertig: id=%s tenant=%s mitarbeiter=%s", viz_id, tid, emp.id)
    return JSONResponse({
        "ok": True, "id": str(viz_id),
        "bild_url": f"/app/api/visualisierungen/{viz_id}/bild",
    })


# =====================================================================
# Kunden-Suche (über Gespräche, Angebote, Rechnungen)
# =====================================================================

@router.get("/kunden")
async def api_kunden(
    request: Request, q: str = "", _e=Depends(require_app_user),
) -> JSONResponse:
    tid = current_tenant_id(request)
    query = (q or "").strip()
    if len(query) < 2:
        return JSONResponse({"q": query, "gespraeche": [], "angebote": [],
                             "rechnungen": [], "hint": "Mind. 2 Zeichen eingeben."})
    like = f"%{query}%"
    async with get_session() as s:
        g = (await s.execute(
            select(Kundengespraech)
            .where(Kundengespraech.tenant_id == tid)
            .where(Kundengespraech.kunde_name.ilike(like))
            .order_by(Kundengespraech.gespraech_datum.desc()).limit(25)
        )).scalars().all()
        a = (await s.execute(
            select(Angebot).where(Angebot.tenant_id == tid)
            .where(Angebot.kunde_name.ilike(like))
            .order_by(Angebot.created_at.desc()).limit(25)
        )).scalars().all()
        r = (await s.execute(
            select(Rechnung).where(Rechnung.tenant_id == tid)
            .where(Rechnung.kunde_name.ilike(like))
            .order_by(Rechnung.created_at.desc()).limit(25)
        )).scalars().all()
    return JSONResponse({
        "q": query,
        "gespraeche": [{"id": str(x.id), "kunde": x.kunde_name,
                        "briefing": (x.briefing_kurz or "")[:140],
                        "zeit": _fmt_dt(x.gespraech_datum)} for x in g],
        "angebote": [{"kunde": x.kunde_name, "betrag": _fmt_eur(x.gesamtbetrag_brutto_eur),
                      "zeit": _fmt_dt(x.created_at)} for x in a],
        "rechnungen": [{"kunde": x.kunde_name or "—", "betrag": _fmt_eur(x.betrag_brutto_eur),
                        "nummer": x.lexware_voucher_number or "",
                        "zeit": _fmt_dt(x.created_at)} for x in r],
    })


@router.get("/kunden/profil")
async def api_kunde_profil(
    request: Request, name: str = "", _e=Depends(require_app_user),
) -> JSONResponse:
    """Gebuendeltes Kundenprofil zu einem (exakten) Namen: Gespraeche,
    Angebote, Rechnungen + Drive-Ordner. Read-only, tenant-gescoped."""
    from core.models.tenant_kunde_drive import TenantKundeDrive
    tid = current_tenant_id(request)
    nm = (name or "").strip()
    if len(nm) < 2:
        return JSONResponse({"ok": False, "error": "Name fehlt."}, status_code=400)
    async with get_session() as s:
        g = (await s.execute(
            select(Kundengespraech)
            .where(Kundengespraech.tenant_id == tid, Kundengespraech.kunde_name.ilike(nm))
            .order_by(Kundengespraech.gespraech_datum.desc()).limit(25)
        )).scalars().all()
        a = (await s.execute(
            select(Angebot)
            .where(Angebot.tenant_id == tid, Angebot.kunde_name.ilike(nm))
            .order_by(Angebot.created_at.desc()).limit(25)
        )).scalars().all()
        r = (await s.execute(
            select(Rechnung)
            .where(Rechnung.tenant_id == tid, Rechnung.kunde_name.ilike(nm))
            .order_by(Rechnung.created_at.desc()).limit(25)
        )).scalars().all()
        drv = (await s.execute(
            select(TenantKundeDrive)
            .where(TenantKundeDrive.tenant_id == tid, TenantKundeDrive.kunde_name.ilike(nm))
            .limit(1)
        )).scalar_one_or_none()

    email = next((x.kunde_email for x in a if getattr(x, "kunde_email", None)), "") or ""
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

    from core.integrations.angebot_mail import send_rechnung_to_customer
    try:
        # send_rechnung_to_customer arbeitet ueber Angebot.lexware_invoice_id —
        # der Helper unterstuetzt aber auch die Rechnung-direkt-Variante
        # via rechnung_id-Parameter. Falls die Funktion das in der aktuellen
        # Version nicht hat, ruft sie eine NotImplementedError → wir geben
        # eine klare Fehlermeldung zurueck.
        try:
            result = await send_rechnung_to_customer(
                rechnung_id=rid, to_email=to_email, cc=cc,
            )
        except TypeError:
            return JSONResponse({
                "ok": False,
                "error": "Rechnungs-Mail erfordert eine in Lexware finalisierte Rechnung. "
                         "Bitte erst in Lexware finalisieren, dann hier senden.",
            }, status_code=400)
    except Exception as exc:  # noqa: BLE001
        logger.exception("send_rechnung crash: %s", exc)
        return JSONResponse({"ok": False, "error": "Mail-Versand fehlgeschlagen."}, status_code=500)

    if not result.get("success"):
        return JSONResponse({
            "ok": False,
            "error": result.get("error") or "Mail-Versand fehlgeschlagen.",
        }, status_code=502)
    return JSONResponse({"ok": True, "message_id": result.get("message_id")})


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
        "heimat_strasse", "heimat_plz", "heimat_ort", "branche",
    }
    from core.models.tenant import Tenant
    async with get_session() as s:
        t = (await s.execute(select(Tenant).where(Tenant.id == tid))).scalar_one_or_none()
        if t is None:
            return JSONResponse({"ok": False, "error": "Tenant nicht gefunden."}, status_code=404)
        for k, v in (body or {}).items():
            if k in allowed:
                val = (v or "").strip() if isinstance(v, str) else v
                setattr(t, k, val or None)
        await s.commit()
    return JSONResponse({"ok": True})


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
    m = await find_oauth_token(tid, "microsoft", employee_id)
    microsoft = {
        "connected": m is not None,
        "account": getattr(m, "account_email", None) if m else None,
        "available": await _microsoft_oauth_available(),
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
        auth_url = await generate_auth_url(
            tenant_slug=tenant.slug, provider=provider, employee_slug=emp.slug,
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
    fuer den Editor. Nur Inhaber, feature-gegated."""
    from core.integrations.anfrage_forms import get_schema_for_tenant, RESERVED_FIELD_NAMES
    from core.models.anfrage import ANFRAGE_TYP_TISCHLER, ANFRAGE_TYP_ALLGEMEIN
    from core.features.check import is_feature_enabled
    tid = current_tenant_id(request)
    if anfrage_typ not in (ANFRAGE_TYP_TISCHLER, ANFRAGE_TYP_ALLGEMEIN):
        return JSONResponse({"ok": False, "error": "Unbekannter Formular-Typ."}, status_code=400)
    if not await is_feature_enabled(tid, _ANFRAGE_FORMULAR_FEATURE):
        return JSONResponse({"ok": False, "error": "Die Anfrage-Formular-Funktion ist nicht aktiv."}, status_code=403)
    schema = await get_schema_for_tenant(tid, anfrage_typ)
    return JSONResponse({
        "ok": True,
        "anfrage_typ": anfrage_typ,
        "title": schema.get("title") or "",
        "subtitle": schema.get("subtitle") or "",
        "fields": schema.get("fields") or [],
        "field_types": _FIELD_TYPE_CHOICES,
        "option_types": sorted(_OPTION_FIELD_TYPES),
        "anfrage_typen": _ANFRAGE_TYP_CHOICES,
        "reserved_names": sorted(RESERVED_FIELD_NAMES),
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

async def _build_command_ctx(request: Request):
    """Baut den Ausfuehrungskontext (tenant-isoliert) fuer das command_center."""
    from core.ai.command_center import Ctx
    from core.features.check import enabled_features_for_tenant

    tid = current_tenant_id(request)
    feats = await enabled_features_for_tenant(tid)
    return Ctx(
        tenant=request.state.app_tenant,
        employee=request.state.app_employee,
        tid=tid,
        features=set(feats),
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

    ctx = await _build_command_ctx(request)
    result = await run_command(text, ctx)
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
    """
    from core.ai.command_center import execute_confirmed

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
