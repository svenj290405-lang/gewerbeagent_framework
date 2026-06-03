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
from core.models.angebot import Angebot
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
from core.models.kundengespraech import Kundengespraech
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

    out = []
    for e in employees:
        ab = absent_map.get(e.id)
        out.append({
            "slug": e.slug,
            "name": e.name,
            "is_inhaber": bool(e.is_default),
            "is_active": bool(e.is_active),
            "job_title": e.job_title or "",
            "skills": list(e.skills or []),
            "kalender_verbunden": bool(e.calendar_provider),
            "app_verbunden": bool(e.telegram_chat_id),  # Geraet/Account gebunden
            "abwesend_heute": ab.absence_type if ab else None,
            "kommende_abwesenheiten": upcoming_by_emp.get(e.id, []),
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
    """Sendet eine Antwort auf eine Anfrage via Microsoft Graph (send_tracked_mail)
    und aktualisiert die EmailConversation (last_message_id, last_q_reply,
    state). Threading-konform: die naechste Kundenantwort wird ueber
    In-Reply-To wieder auf diese Conversation gemappt.

    Body: { "body": "Antwort-Text", "close": true|false }
    - close=true setzt state=closed (Inhaber sagt "Thema erledigt").
    """
    tid = current_tenant_id(request)
    employee = request.state.app_employee  # vom require_app_user gesetzt

    try:
        cid = uuid.UUID(anfrage_id)
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "ungueltige id"}, status_code=400)

    body_data = await request.json()
    reply_text = ((body_data or {}).get("body") or "").strip()
    if not reply_text:
        return JSONResponse({"ok": False, "error": "Leere Antwort."}, status_code=400)
    close_after = bool((body_data or {}).get("close", False))

    async with get_session() as s:
        conv = (await s.execute(
            select(EmailConversation)
            .where(EmailConversation.id == cid)
            .where(EmailConversation.tenant_id == tid)
        )).scalar_one_or_none()
    if conv is None:
        return JSONResponse({"ok": False, "error": "Anfrage nicht gefunden"}, status_code=404)

    # Subject mit Re:-Prefix (RFC 5322 — nur 1x „Re:" zu Beginn).
    base_subject = (conv.last_subject or "Ihre Anfrage").strip()
    if not base_subject.lower().startswith("re:"):
        reply_subject = f"Re: {base_subject}"
    else:
        reply_subject = base_subject

    # Plain → einfacher HTML-Body (Zeilenumbruch zu <br>, leere Zeilen zu <p>).
    paragraphs = [p.strip() for p in reply_text.split("\n\n") if p.strip()]
    body_html = "".join(
        "<p>" + p.replace("\n", "<br>") + "</p>" for p in paragraphs
    )

    # Send via send_tracked_mail (Draft-Create + Send, damit wir die
    # internetMessageId fuer das Threading bekommen). employee_id liefert
    # den richtigen Postfach-Owner falls Multi-OAuth aktiv ist.
    from core.integrations.microsoft import send_tracked_mail
    from core.integrations.mail_pipeline import (
        record_outbound_q_reply,
        set_conversation_state,
    )

    try:
        send_result = await send_tracked_mail(
            tenant_id=tid,
            to_email=conv.kunde_email,
            subject=reply_subject,
            body_html=body_html,
            body_text=reply_text,
            employee_id=getattr(employee, "id", None),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("anfrage-reply send_tracked_mail crash: %s", exc)
        return JSONResponse(
            {"ok": False, "error": "Mail-Versand fehlgeschlagen."},
            status_code=500,
        )

    if not send_result.get("success"):
        return JSONResponse({
            "ok": False,
            "error": send_result.get("error") or "Mail-Versand fehlgeschlagen.",
        }, status_code=502)

    # Conversation-Threading aktualisieren — last_message_id auf die
    # neue internetMessageId, damit der Kunden-Reply darauf zurueck-
    # gemappt wird.
    await record_outbound_q_reply(
        conv_id=cid,
        internet_message_id=send_result.get("internet_message_id"),
        microsoft_conversation_id=send_result.get("conversation_id"),
        q_reply_text=reply_text,
        subject=reply_subject,
    )
    if close_after:
        await set_conversation_state(cid, STATE_CLOSED)

    return JSONResponse({
        "ok": True,
        "internet_message_id": send_result.get("internet_message_id"),
        "closed": close_after,
    })
