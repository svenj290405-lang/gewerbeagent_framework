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
from sqlalchemy import select

from core.database.connection import get_session
from core.models.angebot import Angebot
from core.models.employee import Employee, get_employees_for_tenant
from core.models.employee_absence import (
    get_active_absences,
    get_upcoming_absences,
)
from core.models.kundengespraech import Kundengespraech
from core.models.rechnung import Rechnung
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
