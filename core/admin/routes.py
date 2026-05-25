"""
Admin-Backend Routes.

Mounted unter /admin im Haupt-FastAPI-App.

Setup-Flow:
- /admin/setup     (GET, POST) - nur solange noch kein Admin existiert
- /admin/login     (GET, POST)
- /admin/logout    (POST)
- /admin/          (GET) Dashboard-Overview (Auth required)
- /admin/tenants   (GET) Tenant-Liste
- /admin/tenants/{id} (GET) Detail-View
- /admin/costs     (GET) globale Kosten-Page
- /admin/costs/export.csv (GET)
- /admin/pricing   (GET) Pricing-Editor
- /admin/pricing/update (POST) neuen Preis setzen
- /admin/audit     (GET) Audit-Log
- /admin/sessions/revoke-all (POST)
- /admin/api/feed  (GET, JSON) Live-Feed-Refresh
- /admin/api/health (GET, JSON) Container-Health-Indikator
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import logging
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, desc, func, or_, select, text, update

from core.admin.auth import (
    SESSION_COOKIE_NAME,
    admin_users_exist,
    audit,
    check_login_rate_limit,
    clear_session_cookie,
    create_initial_admin,
    create_session,
    record_login_attempt,
    require_admin,
    require_csrf,
    revoke_all_user_sessions,
    revoke_session,
    set_session_cookie,
    verify_password,
    _client_ip,
)
from core.billing.usage import _invalidate_price_cache
from core.database.connection import get_session
from core.models.admin import (
    AdminAuditLog,
    AdminUser,
    ApiPricingConfig,
    ApiUsageLog,
)
from core.models.anfrage import AnfrageToken, AnfrageResponse
from core.models.tenant import Tenant, TenantStatus
from core.models import Angebot, Beleg, EmailConversation, Kundengespraech, Rechnung
from core.models.email_conversation import (
    STATE_BOOKED,
    STATE_CLOSED,
    STATE_STORNIERT,
)
from core.models.rechnung import RECHNUNG_STATUS_BEZAHLT
from core.models.angebot import (
    ANGEBOT_STATUS_ACCEPTED,
    ANGEBOT_STATUS_RECHNUNG_ERSTELLT,
    ANGEBOT_STATUS_WORK_IN_PROGRESS,
    ANGEBOT_STATUS_WORK_DONE,
    ANGEBOT_STATUS_RECHNUNG_GESENDET,
)

logger = logging.getLogger(__name__)

# ---------- Templates ----------
TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


router = APIRouter(prefix="/admin", tags=["admin"])

# Static-Mount fuer admin.css
def mount_static(app):
    app.mount("/admin/static", StaticFiles(directory=str(STATIC_DIR)), name="admin_static")


# =====================================================================
# UTIL
# =====================================================================

def _today_utc_start() -> dt.datetime:
    n = dt.datetime.now(dt.timezone.utc)
    return n.replace(hour=0, minute=0, second=0, microsecond=0)


def _short_dt(d: dt.datetime | None) -> str:
    if not d:
        return ""
    return d.strftime("%d.%m %H:%M")


def _short_date(d: dt.datetime | None) -> str:
    if not d:
        return ""
    return d.strftime("%d.%m.%Y")


# =====================================================================
# SETUP / LOGIN / LOGOUT
# =====================================================================

@router.get("/setup", response_class=HTMLResponse)
async def setup_get(request: Request):
    if await admin_users_exist():
        # Setup deaktiviert wenn schon ein Admin existiert
        return RedirectResponse("/admin/login", status_code=302)
    return templates.TemplateResponse(request, "setup.html", {"request": request, "user": None})


@router.post("/setup")
async def setup_post(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
):
    if await admin_users_exist():
        raise HTTPException(409, "Setup bereits durchgefuehrt")
    if password != password_confirm:
        return templates.TemplateResponse(request, "setup.html",
            {"request": request, "user": None,
             "flash": {"kind": "error", "message": "Passwoerter stimmen nicht ueberein"}},
            status_code=400,
        )
    user = await create_initial_admin(email=email, password=password, request=request)
    logger.info(f"First admin user created: {user.email}")
    return RedirectResponse("/admin/login", status_code=303)


@router.get("/login", response_class=HTMLResponse)
async def login_get(request: Request):
    # Wenn noch kein Admin existiert -> Setup
    if not await admin_users_exist():
        return RedirectResponse("/admin/setup", status_code=302)
    return templates.TemplateResponse(request, "login.html", {"request": request, "user": None})


@router.post("/login")
async def login_post(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
):
    ip = _client_ip(request)
    email_clean = (email or "").lower().strip()

    async with get_session() as s:
        # Rate-Limit pro IP
        if not await check_login_rate_limit(ip, session=s):
            await audit(
                action="login.rate_limited", target=email_clean,
                request=request, success=False, session=s,
            )
            return templates.TemplateResponse(request, "login.html",
                {"request": request, "user": None,
                 "error": "Zu viele Versuche. Bitte 15 Minuten warten."},
                status_code=429,
            )

        # User holen
        result = await s.execute(
            select(AdminUser).where(AdminUser.email == email_clean)
        )
        user = result.scalar_one_or_none()
        ok = user and user.is_active and verify_password(password, user.password_hash)

        await record_login_attempt(
            ip=ip, email=email_clean, success=bool(ok), session=s,
        )

        if not ok:
            await audit(
                user_id=user.id if user else None,
                action="login.failed", target=email_clean,
                request=request, success=False, session=s,
            )
            return templates.TemplateResponse(request, "login.html",
                {"request": request, "user": None,
                 "error": "E-Mail oder Passwort falsch.",
                 "email_prefill": email_clean},
                status_code=401,
            )

        # Login erfolgreich -> neue Session
        sess = await create_session(user=user, request=request, session=s)
        user.last_login_at = dt.datetime.now(dt.timezone.utc)
        user.last_login_ip = ip
        await audit(
            user_id=user.id, action="login.success",
            request=request, session=s,
        )

        # Cookie setzen + redirect
        await s.flush()
        token = sess.token
        await s.commit()

    response = RedirectResponse("/admin/", status_code=303)
    set_session_cookie(response, token)
    return response


@router.post("/logout")
async def logout_post(
    request: Request,
    user: AdminUser = Depends(require_admin),
    _: None = Depends(require_csrf),
):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        async with get_session() as s:
            await revoke_session(token, session=s)
            await audit(
                user_id=user.id, action="logout", request=request, session=s,
            )
    response = RedirectResponse("/admin/login", status_code=303)
    clear_session_cookie(response)
    return response


@router.post("/sessions/revoke-all")
async def revoke_all_sessions(
    request: Request,
    user: AdminUser = Depends(require_admin),
    _: None = Depends(require_csrf),
):
    async with get_session() as s:
        n = await revoke_all_user_sessions(user.id, session=s)
        await audit(
            user_id=user.id, action="sessions.revoke_all",
            target=str(n), request=request, session=s,
        )
    response = RedirectResponse("/admin/login", status_code=303)
    clear_session_cookie(response)
    return response


# =====================================================================
# DASHBOARD OVERVIEW
# =====================================================================

@router.get("/", response_class=HTMLResponse)
async def overview(
    request: Request,
    user: AdminUser = Depends(require_admin),
):
    today = _today_utc_start()
    week_ago = today - dt.timedelta(days=7)
    month_ago = today - dt.timedelta(days=30)

    async with get_session() as s:
        # Tenant-Counts
        active_tenants = (await s.execute(
            select(func.count(Tenant.id)).where(Tenant.status == TenantStatus.ACTIVE.value)
        )).scalar() or 0
        total_tenants = (await s.execute(select(func.count(Tenant.id)))).scalar() or 0

        # Anfragen
        anfragen_today = (await s.execute(
            select(func.count(AnfrageToken.id))
            .where(AnfrageToken.created_at >= today)
        )).scalar() or 0
        anfragen_week = (await s.execute(
            select(func.count(AnfrageToken.id))
            .where(AnfrageToken.created_at >= week_ago)
        )).scalar() or 0

        # Mails - basieren auf api_usage_log mail_send units
        mails_today = (await s.execute(
            select(func.coalesce(func.sum(ApiUsageLog.units_consumed), 0))
            .where(ApiUsageLog.unit == "mail_send")
            .where(ApiUsageLog.created_at >= today)
        )).scalar() or 0
        mails_week = (await s.execute(
            select(func.coalesce(func.sum(ApiUsageLog.units_consumed), 0))
            .where(ApiUsageLog.unit == "mail_send")
            .where(ApiUsageLog.created_at >= week_ago)
        )).scalar() or 0

        # Kosten
        cost_today = (await s.execute(
            select(func.coalesce(func.sum(ApiUsageLog.cost_eur), 0))
            .where(ApiUsageLog.created_at >= today)
        )).scalar() or 0
        cost_month = (await s.execute(
            select(func.coalesce(func.sum(ApiUsageLog.cost_eur), 0))
            .where(ApiUsageLog.created_at >= month_ago)
        )).scalar() or 0

        # Charts: traffic 30 Tage (mails + anfragen pro Tag)
        traffic_data = await _build_traffic_30d(s, month_ago)
        provider_data = await _build_cost_by_provider_30d(s, month_ago)
        top_tenants_data = await _build_top_tenants_30d(s, month_ago)

        # Live-Feed
        live_feed = await _build_live_feed(s, limit=20)

        await audit(
            user_id=user.id, action="overview.view",
            request=request, session=s,
        )

    csrf_token = request.state.admin_csrf

    return templates.TemplateResponse(request, "overview.html", {
        "request": request,
        "user": user,
        "csrf_token": csrf_token,
        "stats": {
            "active_tenants": active_tenants,
            "tenants_total": total_tenants,
            "mails_today": int(mails_today),
            "mails_week": int(mails_week),
            "anfragen_today": anfragen_today,
            "anfragen_week": anfragen_week,
            "cost_today": float(cost_today),
            "cost_month": float(cost_month),
        },
        "charts": {
            "traffic": traffic_data,
            "provider": provider_data,
            "top_tenants": top_tenants_data,
        },
        "live_feed": live_feed,
    })


async def _build_traffic_30d(s, since: dt.datetime) -> dict:
    """Mails + Anfragen pro Tag, Labels = Tagestrenner."""
    days = []
    for i in range(30):
        d = since + dt.timedelta(days=i)
        days.append(d)

    # Mails
    mails_per_day = await s.execute(
        select(
            func.date_trunc("day", ApiUsageLog.created_at).label("d"),
            func.coalesce(func.sum(ApiUsageLog.units_consumed), 0),
        )
        .where(ApiUsageLog.unit == "mail_send")
        .where(ApiUsageLog.created_at >= since)
        .group_by("d")
        .order_by("d")
    )
    mails_map: dict[str, int] = {}
    for d, c in mails_per_day:
        mails_map[d.strftime("%Y-%m-%d")] = int(c)

    # Anfragen
    anfragen_per_day = await s.execute(
        select(
            func.date_trunc("day", AnfrageToken.created_at).label("d"),
            func.count(AnfrageToken.id),
        )
        .where(AnfrageToken.created_at >= since)
        .group_by("d")
        .order_by("d")
    )
    anfr_map: dict[str, int] = {}
    for d, c in anfragen_per_day:
        anfr_map[d.strftime("%Y-%m-%d")] = int(c)

    labels = [d.strftime("%d.%m") for d in days]
    mails_data = [mails_map.get(d.strftime("%Y-%m-%d"), 0) for d in days]
    anfr_data = [anfr_map.get(d.strftime("%Y-%m-%d"), 0) for d in days]

    return {
        "labels": labels,
        "datasets": [
            {"label": "Mails", "data": mails_data},
            {"label": "Anfragen", "data": anfr_data},
        ],
    }


async def _build_cost_by_provider_30d(s, since: dt.datetime) -> dict:
    rows = await s.execute(
        select(
            ApiUsageLog.provider,
            func.coalesce(func.sum(ApiUsageLog.cost_eur), 0),
        )
        .where(ApiUsageLog.created_at >= since)
        .group_by(ApiUsageLog.provider)
        .order_by(desc(func.sum(ApiUsageLog.cost_eur)))
    )
    labels, data = [], []
    for prov, cost in rows:
        labels.append(prov)
        data.append(float(cost))
    if not labels:
        labels = ["noch keine Daten"]
        data = [1]
    return {"labels": labels, "data": data}


async def _build_top_tenants_30d(s, since: dt.datetime) -> dict:
    rows = await s.execute(
        select(
            Tenant.company_name,
            func.coalesce(func.sum(ApiUsageLog.cost_eur), 0),
        )
        .join(Tenant, ApiUsageLog.tenant_id == Tenant.id)
        .where(ApiUsageLog.created_at >= since)
        .group_by(Tenant.company_name)
        .order_by(desc(func.sum(ApiUsageLog.cost_eur)))
        .limit(10)
    )
    labels, data = [], []
    for name, cost in rows:
        labels.append(name[:30])
        data.append(float(cost))
    if not labels:
        labels = ["keine Tenant-Kosten"]
        data = [0]
    return {"labels": labels, "data": data}


def _pill_for_kind(kind: str) -> str:
    if kind in ("mail",): return "pill-blue"
    if kind in ("anfrage",): return "pill-green"
    if kind in ("api",): return ""
    return ""


async def _build_live_feed(s, limit: int = 20) -> list[dict]:
    """Mischt Anfragen, neueste API-Calls, neueste Audit-Events zu einem Feed."""
    events: list[dict] = []

    # Letzte Anfragen
    rows = (await s.execute(
        select(AnfrageToken).order_by(desc(AnfrageToken.created_at)).limit(10)
    )).scalars().all()
    for a in rows:
        events.append({
            "ts": a.created_at,
            "kind": "anfrage",
            "pill_class": "pill-green",
            "time": _short_dt(a.created_at),
            "message": f"Anfrage von {a.kunde_email or 'unbekannt'}",
        })

    # Letzte Mails
    rows = (await s.execute(
        select(ApiUsageLog)
        .where(ApiUsageLog.unit == "mail_send")
        .order_by(desc(ApiUsageLog.created_at))
        .limit(8)
    )).scalars().all()
    for r in rows:
        events.append({
            "ts": r.created_at,
            "kind": "mail",
            "pill_class": "pill-blue",
            "time": _short_dt(r.created_at),
            "message": f"Mail via {r.provider} ({float(r.units_consumed):.0f}x)",
        })

    # Top API-Calls (groesste Kosten)
    rows = (await s.execute(
        select(ApiUsageLog)
        .where(ApiUsageLog.unit != "mail_send")
        .order_by(desc(ApiUsageLog.created_at))
        .limit(8)
    )).scalars().all()
    for r in rows:
        events.append({
            "ts": r.created_at,
            "kind": "api",
            "pill_class": "",
            "time": _short_dt(r.created_at),
            "message": f"{r.provider}/{r.operation or ''} {float(r.units_consumed):.0f} {r.unit} → {float(r.cost_eur):.6f} €",
        })

    events.sort(key=lambda e: e["ts"], reverse=True)
    return events[:limit]


@router.get("/api/feed")
async def api_feed(
    request: Request,
    user: AdminUser = Depends(require_admin),
):
    async with get_session() as s:
        feed = await _build_live_feed(s, limit=20)
    return JSONResponse({"events": [
        {"time": e["time"], "kind": e["kind"], "pill_class": e["pill_class"], "message": e["message"]}
        for e in feed
    ]})


@router.get("/api/health")
async def api_health(
    request: Request,
    user: AdminUser = Depends(require_admin),
):
    """Health-Probe fuer das Live-Indikator-Dot oben rechts.

    Greift jetzt auch auf den Cron-Heartbeat zu — wenn ein Background-
    Cron seit > Toleranz keinen Heartbeat geschrieben hat, status
    'degraded' mit Liste der toten Crons.
    """
    try:
        async with get_session() as s:
            await s.execute(text("SELECT 1"))
    except Exception as e:
        return {"status": "degraded", "db_error": str(e)[:80]}

    # Cron-Health pruefen
    try:
        from core.integrations.cron_health import get_health_report
        cron_report = get_health_report()
        return {
            "status": cron_report["status"],
            "db": "ok",
            "crons": cron_report["crons"],
        }
    except Exception as e:
        return {"status": "ok", "db": "ok", "cron_check_error": str(e)[:80]}


# =====================================================================
# TENANTS
# =====================================================================

@router.get("/tenants", response_class=HTMLResponse)
async def tenants_list(
    request: Request,
    user: AdminUser = Depends(require_admin),
):
    month_ago = _today_utc_start() - dt.timedelta(days=30)
    async with get_session() as s:
        tenants_raw = (await s.execute(
            select(Tenant).order_by(Tenant.company_name)
        )).scalars().all()

        # Mails / Anfragen / Kosten pro Tenant (30 Tage)
        async def _stats_for(tid: uuid.UUID) -> tuple[int, int, float]:
            mails = (await s.execute(
                select(func.coalesce(func.sum(ApiUsageLog.units_consumed), 0))
                .where(ApiUsageLog.tenant_id == tid)
                .where(ApiUsageLog.unit == "mail_send")
                .where(ApiUsageLog.created_at >= month_ago)
            )).scalar() or 0

            anfr = (await s.execute(
                select(func.count(AnfrageToken.id))
                .where(AnfrageToken.tenant_id == tid)
                .where(AnfrageToken.created_at >= month_ago)
            )).scalar() or 0

            cost = (await s.execute(
                select(func.coalesce(func.sum(ApiUsageLog.cost_eur), 0))
                .where(ApiUsageLog.tenant_id == tid)
                .where(ApiUsageLog.created_at >= month_ago)
            )).scalar() or 0

            return int(mails), int(anfr), float(cost)

        rows = []
        for t in tenants_raw:
            m, a, c = await _stats_for(t.id)
            rows.append({
                "id": str(t.id),
                "slug": t.slug,
                "company_name": t.company_name,
                "contact_name": t.contact_name,
                "contact_email": t.contact_email,
                "branche": t.branche,
                "status": (t.status.value if hasattr(t.status, "value") else t.status),
                "mails_month": m,
                "anfragen_month": a,
                "cost_month": c,
                "created_at_short": _short_date(t.created_at),
            })
        rows.sort(key=lambda r: r["cost_month"], reverse=True)

        await audit(user_id=user.id, action="tenants.list", request=request, session=s)

    return templates.TemplateResponse(request, "tenants.html", {
        "request": request, "user": user, "tenants": rows,
        "csrf_token": request.state.admin_csrf,
    })


@router.get("/tenants/{tenant_id}", response_class=HTMLResponse)
async def tenant_detail(
    request: Request,
    tenant_id: str,
    user: AdminUser = Depends(require_admin),
):
    try:
        tid = uuid.UUID(tenant_id)
    except ValueError:
        raise HTTPException(404, "Tenant nicht gefunden")

    today = _today_utc_start()
    month_ago = today - dt.timedelta(days=30)

    async with get_session() as s:
        tenant = (await s.execute(
            select(Tenant).where(Tenant.id == tid)
        )).scalar_one_or_none()
        if not tenant:
            raise HTTPException(404, "Tenant nicht gefunden")

        # Stats
        mails_month = (await s.execute(
            select(func.coalesce(func.sum(ApiUsageLog.units_consumed), 0))
            .where(ApiUsageLog.tenant_id == tid)
            .where(ApiUsageLog.unit == "mail_send")
            .where(ApiUsageLog.created_at >= month_ago)
        )).scalar() or 0

        mails_total = (await s.execute(
            select(func.coalesce(func.sum(ApiUsageLog.units_consumed), 0))
            .where(ApiUsageLog.tenant_id == tid)
            .where(ApiUsageLog.unit == "mail_send")
        )).scalar() or 0

        anfragen_month = (await s.execute(
            select(func.count(AnfrageToken.id))
            .where(AnfrageToken.tenant_id == tid)
            .where(AnfrageToken.created_at >= month_ago)
        )).scalar() or 0

        anfragen_total = (await s.execute(
            select(func.count(AnfrageToken.id))
            .where(AnfrageToken.tenant_id == tid)
        )).scalar() or 0

        cost_month = (await s.execute(
            select(func.coalesce(func.sum(ApiUsageLog.cost_eur), 0))
            .where(ApiUsageLog.tenant_id == tid)
            .where(ApiUsageLog.created_at >= month_ago)
        )).scalar() or 0

        cost_total = (await s.execute(
            select(func.coalesce(func.sum(ApiUsageLog.cost_eur), 0))
            .where(ApiUsageLog.tenant_id == tid)
        )).scalar() or 0

        # Timeline (30 Tage)
        timeline_rows = await s.execute(
            select(
                func.date_trunc("day", ApiUsageLog.created_at).label("d"),
                func.coalesce(func.sum(ApiUsageLog.cost_eur), 0),
            )
            .where(ApiUsageLog.tenant_id == tid)
            .where(ApiUsageLog.created_at >= month_ago)
            .group_by("d")
            .order_by("d")
        )
        tl_map = {d.strftime("%Y-%m-%d"): float(c) for d, c in timeline_rows}
        labels_30 = [(month_ago + dt.timedelta(days=i)).strftime("%d.%m") for i in range(30)]
        data_30 = [tl_map.get((month_ago + dt.timedelta(days=i)).strftime("%Y-%m-%d"), 0) for i in range(30)]

        # Provider-Pie
        prov_rows = await s.execute(
            select(
                ApiUsageLog.provider,
                func.coalesce(func.sum(ApiUsageLog.cost_eur), 0),
            )
            .where(ApiUsageLog.tenant_id == tid)
            .where(ApiUsageLog.created_at >= month_ago)
            .group_by(ApiUsageLog.provider)
            .order_by(desc(func.sum(ApiUsageLog.cost_eur)))
        )
        prov_labels, prov_data = [], []
        for p, c in prov_rows:
            prov_labels.append(p)
            prov_data.append(float(c))
        if not prov_labels:
            prov_labels = ["keine Calls"]
            prov_data = [1]

        # Letzte 50 Calls
        usage_rows = (await s.execute(
            select(ApiUsageLog)
            .where(ApiUsageLog.tenant_id == tid)
            .order_by(desc(ApiUsageLog.created_at))
            .limit(50)
        )).scalars().all()

        usage = [
            {
                "created_short": _short_dt(u.created_at),
                "provider": u.provider,
                "operation": u.operation,
                "unit": u.unit,
                "units_consumed": u.units_consumed,
                "cost_eur": u.cost_eur,
            }
            for u in usage_rows
        ]

        # Beta-1 B1-10: Mail-Queue-Health pro Tenant
        from core.models import FailedMailQueue
        day_ago = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)
        mq_pending = (await s.execute(
            select(func.count(FailedMailQueue.id))
            .where(FailedMailQueue.tenant_id == tid)
            .where(FailedMailQueue.status == "pending")
        )).scalar() or 0
        mq_sent_24h = (await s.execute(
            select(func.count(FailedMailQueue.id))
            .where(FailedMailQueue.tenant_id == tid)
            .where(FailedMailQueue.status == "sent")
            .where(FailedMailQueue.updated_at >= day_ago)
        )).scalar() or 0
        mq_dead = (await s.execute(
            select(func.count(FailedMailQueue.id))
            .where(FailedMailQueue.tenant_id == tid)
            .where(FailedMailQueue.status == "dead")
        )).scalar() or 0

        # Eigener Telegram-Bot pro Betrieb: aktueller Token-Status
        from core.models import ToolConfig
        tg_tc = (await s.execute(
            select(ToolConfig).where(
                ToolConfig.tenant_id == tid,
                ToolConfig.tool_name == "telegram_notify",
            )
        )).scalar_one_or_none()
        from core.security.encryption import try_decrypt
        _tg_tok = try_decrypt((tg_tc.config or {}).get("bot_token")) if tg_tc else None
        telegram_bot_set = bool(_tg_tok)
        telegram_bot_hint = (
            f"…{_tg_tok[-6:]}" if _tg_tok and len(_tg_tok) > 6 else ""
        )

        await audit(
            user_id=user.id, action="tenant.view",
            target=tenant.slug, request=request, session=s,
        )

    return templates.TemplateResponse(request, "tenant_detail.html", {
        "request": request, "user": user,
        "tenant": tenant,
        "stats": {
            "mails_month": int(mails_month),
            "mails_total": int(mails_total),
            "anfragen_month": int(anfragen_month),
            "anfragen_total": int(anfragen_total),
            "cost_month": float(cost_month),
            "cost_total": float(cost_total),
            "mq_pending": int(mq_pending),
            "mq_sent_24h": int(mq_sent_24h),
            "mq_dead": int(mq_dead),
        },
        "charts": {
            "timeline": {"labels": labels_30, "data": data_30},
            "providers": {"labels": prov_labels, "data": prov_data},
        },
        "usage": usage,
        "telegram_bot_set": telegram_bot_set,
        "telegram_bot_hint": telegram_bot_hint,
        "tgbot_msg": request.query_params.get("tgbot"),
        "csrf_token": request.state.admin_csrf,
    })


# =====================================================================
# TENANT FEATURES (Per-Feature-Toggle)
# =====================================================================

@router.get(
    "/tenants/{tenant_id}/features", response_class=HTMLResponse,
)
async def tenant_features_page(
    request: Request,
    tenant_id: str,
    user: AdminUser = Depends(require_admin),
):
    """Feature-Toggle-Seite pro Tenant.

    Listet alle Catalog-Features mit Per-Feature-Toggle. Es gibt keine
    Pakete/Tiers mehr — jedes Feature wird einzeln geschaltet.
    """
    from core.features import FEATURES, enabled_features_for_tenant

    try:
        tid = uuid.UUID(tenant_id)
    except ValueError:
        raise HTTPException(404, "Tenant nicht gefunden")

    async with get_session() as s:
        tenant = (await s.execute(
            select(Tenant).where(Tenant.id == tid)
        )).scalar_one_or_none()
        if not tenant:
            raise HTTPException(404, "Tenant nicht gefunden")

        await audit(
            user_id=user.id, action="tenant.features.view",
            target=tenant.slug, request=request, session=s,
        )

    enabled = await enabled_features_for_tenant(tid)

    def _row(f):
        return {
            "key": f.key,
            "label": f.label,
            "description": f.description,
            "enabled": f.always_on or f.key in enabled,
            "always_on": f.always_on,
        }

    sorted_features = sorted(FEATURES.values(), key=lambda x: x.label)
    sections = [
        {
            "label": "Immer aktiv",
            "rows": [_row(f) for f in sorted_features if f.always_on],
        },
        {
            "label": "Features",
            "rows": [_row(f) for f in sorted_features if not f.always_on],
        },
    ]

    return templates.TemplateResponse(request, "tenant_features.html", {
        "request": request, "user": user,
        "tenant": tenant,
        "sections": sections,
        "csrf_token": request.state.admin_csrf,
    })


@router.post("/tenants/{tenant_id}/retention")
async def tenant_set_retention(
    request: Request,
    tenant_id: str,
    data_retention_days: int = Form(...),
    csrf_token: str = Form(...),
    user: AdminUser = Depends(require_admin),
):
    """Phase B4: data_retention_days fuer Tenant aendern.

    Range 7-365. dsgvo_cleanup_cron nimmt den Wert beim naechsten 03:00-
    Tick auf — kein Restart noetig.
    """
    await require_csrf(request)
    if data_retention_days < 7 or data_retention_days > 365:
        raise HTTPException(400, "Retention muss zwischen 7 und 365 Tagen liegen")
    try:
        tid = uuid.UUID(tenant_id)
    except ValueError:
        raise HTTPException(404, "Tenant nicht gefunden")
    async with get_session() as s:
        tenant = (await s.execute(
            select(Tenant).where(Tenant.id == tid)
        )).scalar_one_or_none()
        if not tenant:
            raise HTTPException(404, "Tenant nicht gefunden")
        old = tenant.data_retention_days
        tenant.data_retention_days = data_retention_days
        await audit(
            user_id=user.id, action="tenant.retention.update",
            target=tenant.slug, request=request, session=s,
            details={"old": old, "new": data_retention_days},
        )
        await s.commit()
    return RedirectResponse(
        f"/admin/tenants/{tenant_id}", status_code=303,
    )


@router.post("/tenants/{tenant_id}/telegram-bot")
async def tenant_set_telegram_bot(
    request: Request,
    tenant_id: str,
    bot_token: str = Form(""),
    csrf_token: str = Form(...),
    user: AdminUser = Depends(require_admin),
):
    """Eigener Telegram-Bot pro Betrieb (Variante A): bot_token setzen oder
    leeren. Bei gesetztem Token wird ausserdem der Webhook dieses Bots auf
    den tenant-spezifischen Pfad registriert, damit eingehende Updates dem
    Betrieb zugeordnet werden und Antworten ueber diesen Bot rausgehen.
    Leeres Feld = Token entfernen (Betrieb faellt auf den geteilten globalen
    Bot zurueck)."""
    await require_csrf(request)
    token = (bot_token or "").strip()
    try:
        tid = uuid.UUID(tenant_id)
    except ValueError:
        raise HTTPException(404, "Tenant nicht gefunden")
    from core.models import ToolConfig
    async with get_session() as s:
        tenant = (await s.execute(
            select(Tenant).where(Tenant.id == tid)
        )).scalar_one_or_none()
        if not tenant:
            raise HTTPException(404, "Tenant nicht gefunden")
        slug = tenant.slug
        tc = (await s.execute(
            select(ToolConfig).where(
                ToolConfig.tenant_id == tid,
                ToolConfig.tool_name == "telegram_notify",
            )
        )).scalar_one_or_none()
        had = bool((tc.config or {}).get("bot_token")) if tc else False

    # Speichern (verschluesselt) + Webhook setzen — gemeinsame Logik mit
    # dem Self-Service-Chat-Flow (/eigenen_bot).
    from plugins.telegram_notify.handler import provision_tenant_bot
    note = await provision_tenant_bot(tid, slug, token)

    async with get_session() as s:
        await audit(
            user_id=user.id, action="tenant.telegram_bot.update",
            target=slug, request=request, session=s,
            details={"had_token": had, "now_set": bool(token)},
        )
        await s.commit()
    logger.info(f"Tenant-Bot-Update {slug}: {note}")

    from urllib.parse import quote
    return RedirectResponse(
        f"/admin/tenants/{tenant_id}?tgbot={quote(note)}", status_code=303,
    )


@router.post("/tenants/{tenant_id}/features/{feature_key}/toggle")
async def tenant_features_toggle(
    request: Request,
    tenant_id: str,
    feature_key: str,
    csrf_token: str = Form(...),
    user: AdminUser = Depends(require_admin),
):
    """Togglet ein einzelnes Feature fuer den Tenant."""
    await require_csrf(request)
    from core.features import FEATURES, invalidate_feature_cache
    from core.models import ToolConfig

    if feature_key not in FEATURES:
        raise HTTPException(400, f"Unbekanntes Feature: {feature_key}")

    feature = FEATURES[feature_key]
    if feature.always_on:
        raise HTTPException(400, "Always-on-Feature kann nicht getoggled werden")

    try:
        tid = uuid.UUID(tenant_id)
    except ValueError:
        raise HTTPException(404, "Tenant nicht gefunden")

    async with get_session() as s:
        tenant = (await s.execute(
            select(Tenant).where(Tenant.id == tid)
        )).scalar_one_or_none()
        if not tenant:
            raise HTTPException(404, "Tenant nicht gefunden")

        # ToolConfig holen oder anlegen
        tc = (await s.execute(
            select(ToolConfig)
            .where(ToolConfig.tenant_id == tid)
            .where(ToolConfig.tool_name == feature_key)
        )).scalar_one_or_none()

        if tc is None:
            tc = ToolConfig(
                tenant_id=tid,
                tool_name=feature_key,
                enabled=True,
                config={},
            )
            old_value = False
            s.add(tc)
        else:
            old_value = tc.enabled
            tc.enabled = not tc.enabled

        new_value = tc.enabled

        await audit(
            user_id=user.id, action="tenant.features.toggle",
            target=f"{tenant.slug}:{feature_key}", request=request, session=s,
            details={"old": old_value, "new": new_value},
        )
        await s.commit()

    invalidate_feature_cache(tid)

    return RedirectResponse(
        f"/admin/tenants/{tenant_id}/features", status_code=303,
    )


# =====================================================================
# COSTS GLOBAL
# =====================================================================

@router.get("/costs", response_class=HTMLResponse)
async def costs_global(
    request: Request,
    user: AdminUser = Depends(require_admin),
):
    today_start = _today_utc_start()
    week_ago = today_start - dt.timedelta(days=7)
    month_ago = today_start - dt.timedelta(days=30)
    year_start = today_start.replace(month=1, day=1)

    async def _agg(s, since: dt.datetime) -> tuple[float, int]:
        cost = (await s.execute(
            select(func.coalesce(func.sum(ApiUsageLog.cost_eur), 0))
            .where(ApiUsageLog.created_at >= since)
        )).scalar() or 0
        calls = (await s.execute(
            select(func.count(ApiUsageLog.id))
            .where(ApiUsageLog.created_at >= since)
        )).scalar() or 0
        return float(cost), int(calls)

    async with get_session() as s:
        today_c, today_n = await _agg(s, today_start)
        week_c, week_n = await _agg(s, week_ago)
        month_c, month_n = await _agg(s, month_ago)
        year_c, year_n = await _agg(s, year_start)

        # By provider/operation 30 Tage
        rows = await s.execute(
            select(
                ApiUsageLog.provider,
                ApiUsageLog.operation,
                func.coalesce(func.sum(ApiUsageLog.units_consumed), 0),
                func.count(ApiUsageLog.id),
                func.coalesce(func.sum(ApiUsageLog.cost_eur), 0),
            )
            .where(ApiUsageLog.created_at >= month_ago)
            .group_by(ApiUsageLog.provider, ApiUsageLog.operation)
            .order_by(desc(func.sum(ApiUsageLog.cost_eur)))
        )
        by_provider = [
            {"provider": p, "operation": o, "units": u, "calls": c, "cost": cost}
            for p, o, u, c, cost in rows
        ]

        # By tenant 30 Tage
        rows = await s.execute(
            select(
                Tenant.company_name,
                Tenant.slug,
                func.count(ApiUsageLog.id),
                func.coalesce(func.sum(ApiUsageLog.cost_eur), 0),
            )
            .outerjoin(Tenant, ApiUsageLog.tenant_id == Tenant.id)
            .where(ApiUsageLog.created_at >= month_ago)
            .group_by(Tenant.company_name, Tenant.slug)
            .order_by(desc(func.sum(ApiUsageLog.cost_eur)))
            .limit(50)
        )
        by_tenant = [
            {"company_name": n, "slug": s_slug, "calls": c, "cost": cost}
            for n, s_slug, c, cost in rows
        ]

        await audit(user_id=user.id, action="costs.view", request=request, session=s)

    return templates.TemplateResponse(request, "costs.html", {
        "request": request, "user": user,
        "csrf_token": request.state.admin_csrf,
        "totals": {
            "today": today_c, "today_calls": today_n,
            "week": week_c, "week_calls": week_n,
            "month": month_c, "month_calls": month_n,
            "year": year_c, "year_calls": year_n,
        },
        "by_provider": by_provider,
        "by_tenant": by_tenant,
    })


@router.get("/costs/export.csv")
async def costs_export(
    request: Request,
    user: AdminUser = Depends(require_admin),
):
    """CSV-Export aller Usage-Zeilen letzte 30 Tage."""
    month_ago = _today_utc_start() - dt.timedelta(days=30)
    buf = io.StringIO()
    writer = csv.writer(buf, dialect="excel-tab")
    writer.writerow([
        "created_at", "tenant", "tenant_slug", "provider", "operation",
        "unit", "units_consumed", "price_per_unit_eur", "cost_eur",
    ])

    async with get_session() as s:
        rows = await s.execute(
            select(
                ApiUsageLog.created_at,
                Tenant.company_name,
                Tenant.slug,
                ApiUsageLog.provider,
                ApiUsageLog.operation,
                ApiUsageLog.unit,
                ApiUsageLog.units_consumed,
                ApiUsageLog.price_per_unit_eur,
                ApiUsageLog.cost_eur,
            )
            .outerjoin(Tenant, ApiUsageLog.tenant_id == Tenant.id)
            .where(ApiUsageLog.created_at >= month_ago)
            .order_by(desc(ApiUsageLog.created_at))
        )
        for r in rows:
            writer.writerow([
                r[0].isoformat() if r[0] else "",
                r[1] or "",
                r[2] or "",
                r[3] or "",
                r[4] or "",
                r[5] or "",
                str(r[6] or ""),
                str(r[7] or ""),
                str(r[8] or ""),
            ])
        await audit(user_id=user.id, action="costs.export", request=request, session=s)

    fname = f"gewerbeagent-kosten-{dt.datetime.now().strftime('%Y%m%d')}.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/tab-separated-values",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# =====================================================================
# PRICING
# =====================================================================

@router.get("/pricing", response_class=HTMLResponse)
async def pricing_view(
    request: Request,
    user: AdminUser = Depends(require_admin),
):
    async with get_session() as s:
        now = dt.datetime.now(dt.timezone.utc)
        current = (await s.execute(
            select(ApiPricingConfig)
            .where(ApiPricingConfig.valid_from <= now)
            .where(or_(
                ApiPricingConfig.valid_to.is_(None),
                ApiPricingConfig.valid_to > now,
            ))
            .order_by(ApiPricingConfig.provider, ApiPricingConfig.operation)
        )).scalars().all()

        history = (await s.execute(
            select(ApiPricingConfig)
            .where(ApiPricingConfig.valid_to.is_not(None))
            .where(ApiPricingConfig.valid_to <= now)
            .order_by(desc(ApiPricingConfig.valid_to))
            .limit(100)
        )).scalars().all()

        await audit(user_id=user.id, action="pricing.view", request=request, session=s)

    def _fmt(p):
        return {
            "provider": p.provider,
            "operation": p.operation,
            "unit": p.unit,
            "price_per_unit_eur": p.price_per_unit_eur,
            "valid_from_short": _short_dt(p.valid_from),
            "valid_to_short": _short_dt(p.valid_to) if p.valid_to else None,
            "notes": p.notes,
        }

    return templates.TemplateResponse(request, "pricing.html", {
        "request": request, "user": user,
        "csrf_token": request.state.admin_csrf,
        "current": [_fmt(p) for p in current],
        "history": [_fmt(p) for p in history],
    })


@router.post("/pricing/update")
async def pricing_update(
    request: Request,
    provider: str = Form(...),
    operation: str = Form(""),
    unit: str = Form(...),
    new_price: str = Form(...),
    notes: str = Form(""),
    user: AdminUser = Depends(require_admin),
    _: None = Depends(require_csrf),
):
    try:
        price = Decimal(new_price)
        if price < 0:
            raise ValueError
    except Exception:
        raise HTTPException(400, "Preis ungueltig")

    op_norm = operation.strip() or None
    now = dt.datetime.now(dt.timezone.utc)

    async with get_session() as s:
        # alte Preise schliessen
        await s.execute(
            update(ApiPricingConfig)
            .where(ApiPricingConfig.provider == provider)
            .where(ApiPricingConfig.unit == unit)
            .where(ApiPricingConfig.operation == op_norm)
            .where(ApiPricingConfig.valid_to.is_(None))
            .values(valid_to=now)
        )
        # neue Zeile
        new_row = ApiPricingConfig(
            provider=provider, operation=op_norm, unit=unit,
            price_per_unit_eur=price,
            valid_from=now,
            valid_to=None,
            notes=notes.strip() or None,
            created_by=user.id,
        )
        s.add(new_row)
        await audit(
            user_id=user.id, action="pricing.update",
            target=f"{provider}/{op_norm}/{unit}",
            request=request, session=s,
            details={"new_price_eur": str(price), "notes": notes},
        )

    _invalidate_price_cache()
    return RedirectResponse("/admin/pricing", status_code=303)


# =====================================================================
# AUDIT LOG VIEW
# =====================================================================

@router.get("/audit", response_class=HTMLResponse)
async def audit_view(
    request: Request,
    user: AdminUser = Depends(require_admin),
):
    async with get_session() as s:
        rows = await s.execute(
            select(AdminAuditLog, AdminUser.email)
            .outerjoin(AdminUser, AdminAuditLog.user_id == AdminUser.id)
            .order_by(desc(AdminAuditLog.created_at))
            .limit(200)
        )
        events = []
        for ev, email in rows:
            events.append({
                "created_short": _short_dt(ev.created_at),
                "user_email": email,
                "action": ev.action,
                "target": ev.target,
                "ip_address": ev.ip_address,
                "success": ev.success,
            })

    return templates.TemplateResponse(request, "audit.html", {
        "request": request, "user": user,
        "csrf_token": request.state.admin_csrf,
        "events": events,
    })


# =====================================================================
# METRIKEN — Business-Nutzung pro Betrieb (Grundlage Preisverhandlung)
# =====================================================================

# (key, Spaltenkopf, fmt) — fmt: int | eur | cost | h. Reihenfolge =
# Spaltenreihenfolge in Tabelle + CSV.
_METRIC_COLUMNS = [
    ("anrufe", "Anrufe", "int"),
    ("anruf_min", "Tel.-Min.", "int"),
    ("mails_empf", "Mails empf.", "int"),
    ("mails_bearb", "Mails bearb.", "int"),
    ("mails_send", "Mails vers.", "int"),
    ("kunden_neu", "Kunden angel.", "int"),
    ("anfragen", "Anfragen", "int"),
    ("anfragen_beantw", "Anfr. beantw.", "int"),
    ("webformulare", "Webform. verarb.", "int"),
    ("buchungen", "Buchungen", "int"),
    ("stornos", "Stornos", "int"),
    ("ausserhalb_gz", "Außerh. GZ", "int"),
    ("angebote", "Angebote", "int"),
    ("angebote_angen", "Angeb. angen.", "int"),
    ("angebot_volumen_eur", "Angebotsvol.", "eur"),
    ("rechnungen", "Rechnungen", "int"),
    ("rechnungen_bezahlt", "Rechn. bez.", "int"),
    ("umsatz_eur", "Umsatz ges.", "eur"),
    ("umsatz_bezahlt_eur", "Umsatz bez.", "eur"),
    ("belege", "Belege", "int"),
    ("kosten", "API-Kosten", "cost"),
]

# Key -> (Header, fmt) fuer schnelle Lookups (z.B. gruppierte Detailansicht).
_METRIC_META = {k: (h, f) for k, h, f in _METRIC_COLUMNS}

# Thematische Gruppierung fuer die Einzel-Betrieb-Detailansicht: ALLE
# Kennzahlen als Karten-Raster auf einer Seite (statt scrollbarer Tabelle).
_METRIC_GROUPS = [
    ("Kommunikation",
     ["anrufe", "anruf_min", "mails_empf", "mails_bearb", "mails_send"]),
    ("Anfragen & Termine",
     ["anfragen", "anfragen_beantw", "webformulare", "buchungen",
      "stornos", "ausserhalb_gz"]),
    ("Kunden & Angebote",
     ["kunden_neu", "angebote", "angebote_angen", "angebot_volumen_eur"]),
    ("Umsatz & Belege",
     ["rechnungen", "rechnungen_bezahlt", "umsatz_eur",
      "umsatz_bezahlt_eur", "belege"]),
    ("System", ["kosten"]),
]


def _build_metric_groups() -> list[tuple]:
    """Gruppen mit aufgeloesten (key, header, fmt)-Tripeln fuers Template."""
    return [
        (title, [(k, _METRIC_META[k][0], _METRIC_META[k][1]) for k in keys])
        for title, keys in _METRIC_GROUPS
    ]


_PERIODS = [(30, "30 Tage"), (90, "90 Tage"), (365, "12 Monate"), (0, "Gesamt")]

# Angebot-Status, die als "angenommen" zaehlen (accepted + Folgezustaende).
_ANGEBOT_ANGENOMMEN = [
    ANGEBOT_STATUS_ACCEPTED,
    ANGEBOT_STATUS_RECHNUNG_ERSTELLT,
    ANGEBOT_STATUS_WORK_IN_PROGRESS,
    ANGEBOT_STATUS_WORK_DONE,
    ANGEBOT_STATUS_RECHNUNG_GESENDET,
]



def _metrics_days(request: Request) -> int:
    try:
        days = int(request.query_params.get("days", "30"))
    except (TypeError, ValueError):
        days = 30
    return days if days in (30, 90, 365, 0) else 30


def _metrics_tenant_id(request: Request) -> uuid.UUID | None:
    """Optionaler ?tenant=<uuid>-Filter fuer den Drill-down auf einen Betrieb."""
    raw = request.query_params.get("tenant")
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except (ValueError, TypeError):
        return None


def _after_hours(ts):
    """Bedingung: Zeitstempel liegt ausserhalb Mo-Fr 8-18 Uhr
    (Europe/Berlin) — Abende, Naechte, Wochenenden. ts ist timestamptz."""
    local = func.timezone("Europe/Berlin", ts)
    return or_(
        func.date_part("isodow", local) > 5,   # Samstag (6) / Sonntag (7)
        func.date_part("hour", local) < 8,
        func.date_part("hour", local) >= 18,
    )


async def _collect_metrics(
    s, start: dt.datetime | None, tenant_id: uuid.UUID | None = None,
) -> tuple[list[dict], dict]:
    """Business-Metriken pro Tenant. Eine gruppierte Aggregat-Query je
    Metrik (statt N-pro-Tenant). start=None => Gesamt-Zeitraum (kein
    created-Filter). tenant_id gesetzt => nur dieser eine Betrieb
    (Drill-down). Liefert (rows, totals)."""

    async def grouped(value_expr, tenant_col, created_col, *wheres) -> dict:
        q = select(tenant_col, value_expr).group_by(tenant_col)
        if start is not None and created_col is not None:
            q = q.where(created_col >= start)
        if tenant_id is not None:
            q = q.where(tenant_col == tenant_id)
        for w in wheres:
            q = q.where(w)
        return {row[0]: row[1] for row in (await s.execute(q))}

    # --- Kommunikation ---
    anrufe = await grouped(
        func.count(Kundengespraech.id), Kundengespraech.tenant_id,
        Kundengespraech.gespraech_datum)
    sekunden = await grouped(
        func.coalesce(func.sum(Kundengespraech.audio_dauer_sekunden), 0),
        Kundengespraech.tenant_id, Kundengespraech.gespraech_datum)
    mails_empf = await grouped(
        func.count(EmailConversation.id), EmailConversation.tenant_id,
        EmailConversation.created_at)
    mails_bearb = await grouped(
        func.count(EmailConversation.id), EmailConversation.tenant_id,
        EmailConversation.created_at,
        EmailConversation.state.in_([STATE_BOOKED, STATE_CLOSED]))
    mails_send = await grouped(
        func.coalesce(func.sum(ApiUsageLog.units_consumed), 0),
        ApiUsageLog.tenant_id, ApiUsageLog.created_at,
        ApiUsageLog.unit == "mail_send")
    kunden_neu = await grouped(
        func.coalesce(func.sum(ApiUsageLog.units_consumed), 0),
        ApiUsageLog.tenant_id, ApiUsageLog.created_at,
        ApiUsageLog.unit == "kunde_neu")

    # --- Anfragen / Termine ---
    anfragen = await grouped(
        func.count(AnfrageToken.id), AnfrageToken.tenant_id,
        AnfrageToken.created_at)
    anfragen_beantw = await grouped(
        func.count(AnfrageToken.id), AnfrageToken.tenant_id,
        AnfrageToken.submitted_at, AnfrageToken.submitted_at.isnot(None))
    # Verarbeitete Webformulare = tatsaechlich eingegangene Formular-
    # Antworten. AnfrageResponse hat keine tenant_id -> Join ueber Token.
    wf_q = (
        select(AnfrageToken.tenant_id, func.count(AnfrageResponse.id))
        .select_from(AnfrageResponse)
        .join(AnfrageToken, AnfrageResponse.token_id == AnfrageToken.id)
        .group_by(AnfrageToken.tenant_id)
    )
    if start is not None:
        wf_q = wf_q.where(AnfrageResponse.submitted_at >= start)
    if tenant_id is not None:
        wf_q = wf_q.where(AnfrageToken.tenant_id == tenant_id)
    webformulare = {row[0]: row[1] for row in (await s.execute(wf_q))}
    buchungen = await grouped(
        func.count(EmailConversation.id), EmailConversation.tenant_id,
        EmailConversation.created_at,
        EmailConversation.gcal_event_id.isnot(None))
    stornos = await grouped(
        func.count(EmailConversation.id), EmailConversation.tenant_id,
        EmailConversation.created_at,
        EmailConversation.state == STATE_STORNIERT)

    # --- Lexware / Umsatz ---
    angebote = await grouped(
        func.count(Angebot.id), Angebot.tenant_id, Angebot.created_at)
    angebote_angen = await grouped(
        func.count(Angebot.id), Angebot.tenant_id, Angebot.created_at,
        Angebot.status.in_(_ANGEBOT_ANGENOMMEN))
    angebot_volumen = await grouped(
        func.coalesce(func.sum(Angebot.gesamtbetrag_brutto_eur), 0),
        Angebot.tenant_id, Angebot.created_at)
    rechnungen = await grouped(
        func.count(Rechnung.id), Rechnung.tenant_id, Rechnung.created_at)
    rechnungen_bezahlt = await grouped(
        func.count(Rechnung.id), Rechnung.tenant_id, Rechnung.created_at,
        Rechnung.status == RECHNUNG_STATUS_BEZAHLT)
    umsatz = await grouped(
        func.coalesce(func.sum(Rechnung.betrag_brutto_eur), 0),
        Rechnung.tenant_id, Rechnung.created_at)
    umsatz_bezahlt = await grouped(
        func.coalesce(func.sum(Rechnung.betrag_brutto_eur), 0),
        Rechnung.tenant_id, Rechnung.created_at,
        Rechnung.status == RECHNUNG_STATUS_BEZAHLT)
    belege = await grouped(
        func.count(Beleg.id), Beleg.tenant_id, Beleg.created_at)
    kosten = await grouped(
        func.coalesce(func.sum(ApiUsageLog.cost_eur), 0),
        ApiUsageLog.tenant_id, ApiUsageLog.created_at)

    # --- 24/7: Vorgaenge ausserhalb der Geschaeftszeiten ---
    ah_calls = await grouped(
        func.count(Kundengespraech.id), Kundengespraech.tenant_id,
        Kundengespraech.gespraech_datum,
        _after_hours(Kundengespraech.gespraech_datum))
    ah_mails = await grouped(
        func.count(EmailConversation.id), EmailConversation.tenant_id,
        EmailConversation.created_at,
        _after_hours(EmailConversation.created_at))
    ah_anfr = await grouped(
        func.count(AnfrageToken.id), AnfrageToken.tenant_id,
        AnfrageToken.created_at, _after_hours(AnfrageToken.created_at))

    tq = select(Tenant).order_by(Tenant.company_name)
    if tenant_id is not None:
        tq = tq.where(Tenant.id == tenant_id)
    tenants = (await s.execute(tq)).scalars().all()

    def geti(d: dict, tid) -> int:
        return int(d.get(tid, 0) or 0)

    def getf(d: dict, tid) -> float:
        return float(d.get(tid, 0) or 0)

    rows: list[dict] = []
    totals = {k: 0 for k, _h, _fmt in _METRIC_COLUMNS}
    for t in tenants:
        tid = t.id
        anruf_min = round(geti(sekunden, tid) / 60)
        m_bearb = geti(mails_bearb, tid)
        anfr = geti(anfragen, tid)
        buch = geti(buchungen, tid)
        ang = geti(angebote, tid)
        rech = geti(rechnungen, tid)
        bel = geti(belege, tid)
        row = {
            "id": str(tid),
            "company_name": t.company_name,
            "slug": t.slug,
            "anrufe": geti(anrufe, tid),
            "anruf_min": anruf_min,
            "mails_empf": geti(mails_empf, tid),
            "mails_bearb": m_bearb,
            "mails_send": geti(mails_send, tid),
            "kunden_neu": geti(kunden_neu, tid),
            "anfragen": anfr,
            "anfragen_beantw": geti(anfragen_beantw, tid),
            "webformulare": geti(webformulare, tid),
            "buchungen": buch,
            "stornos": geti(stornos, tid),
            "ausserhalb_gz": (
                geti(ah_calls, tid) + geti(ah_mails, tid) + geti(ah_anfr, tid)
            ),
            "angebote": ang,
            "angebote_angen": geti(angebote_angen, tid),
            "angebot_volumen_eur": getf(angebot_volumen, tid),
            "rechnungen": rech,
            "rechnungen_bezahlt": geti(rechnungen_bezahlt, tid),
            "umsatz_eur": getf(umsatz, tid),
            "umsatz_bezahlt_eur": getf(umsatz_bezahlt, tid),
            "belege": bel,
            "kosten": getf(kosten, tid),
        }
        for k, _h, _fmt in _METRIC_COLUMNS:
            totals[k] += row[k]
        rows.append(row)
    return rows, totals


def _eur0(x) -> str:
    """Ganze Euro mit Tausenderpunkt: 12345.6 -> '12.346 €'."""
    return f"{round(x):,}".replace(",", ".") + " €"


def _n0(x) -> str:
    """Ganze Zahl mit Tausenderpunkt: 12345 -> '12.345'."""
    return f"{int(round(x)):,}".replace(",", ".")


def _build_tiles(totals: dict) -> list[dict]:
    """Headline-Wert-Kacheln (Plattform-Summe im Zeitraum). Reine Zahlen,
    im Verkaufsgespraech zeigbar — operative Zaehler zuerst, dann Umsatz."""
    tel_std = round(totals["anruf_min"] / 60)
    return [
        {"label": "Minuten am Telefon",
         "value": _n0(totals["anruf_min"]),
         "sub": f'{_n0(totals["anrufe"])} Anrufe entgegengenommen (≈ {tel_std} Std.)'},
        {"label": "Mails eingegangen",
         "value": _n0(totals["mails_empf"]),
         "sub": "automatisch gelesen & einsortiert"},
        {"label": "Mails bearbeitet",
         "value": _n0(totals["mails_bearb"]),
         "sub": "bis zu Buchung / Abschluss geführt"},
        {"label": "Mails versendet",
         "value": _n0(totals["mails_send"]),
         "sub": "automatisch beantwortet"},
        {"label": "Webformulare verarbeitet",
         "value": _n0(totals["webformulare"]),
         "sub": "ausgefüllte Anfrage-Formulare eingelesen"},
        {"label": "Kunden angelegt",
         "value": _n0(totals["kunden_neu"]),
         "sub": "neue Kontakte in Lexware"},
        {"label": "Rechnungen erstellt",
         "value": _n0(totals["rechnungen"]),
         "sub": f'{_n0(totals["rechnungen_bezahlt"])} bereits bezahlt'},
        {"label": "Termine gebucht",
         "value": _n0(totals["buchungen"]),
         "sub": f'{_n0(totals["stornos"])} storniert'},
        {"label": "Angebote erstellt",
         "value": _n0(totals["angebote"]),
         "sub": f'{_n0(totals["angebote_angen"])} angenommen'},
        {"label": "Anfragen erfasst",
         "value": _n0(totals["anfragen"]),
         "sub": f'{_n0(totals["anfragen_beantw"])} vollständig beantwortet'},
        {"label": "Außerhalb der Geschäftszeiten",
         "value": _n0(totals["ausserhalb_gz"]),
         "sub": "Abende & Wochenenden erledigt — 24/7"},
    ]


def _build_charts(rows: list[dict], totals: dict) -> dict:
    """Daten fuer die Chart.js-Visualisierungen: Plattform-Leistung als
    Balken + Aktivitaet je Betrieb (Top 12 nach Gesamt-Vorgaengen)."""
    plattform = {
        "labels": ["Anrufe", "Mails eing.", "Mails bearb.", "Mails vers.",
                   "Webform.", "Kunden", "Rechn.", "Termine", "Angebote",
                   "Anfragen"],
        "data": [totals["anrufe"], totals["mails_empf"], totals["mails_bearb"],
                 totals["mails_send"], totals["webformulare"],
                 totals["kunden_neu"], totals["rechnungen"],
                 totals["buchungen"], totals["angebote"], totals["anfragen"]],
    }

    def aktivitaet(r: dict) -> int:
        return (r["anrufe"] + r["mails_bearb"] + r["mails_send"]
                + r["webformulare"] + r["buchungen"] + r["rechnungen"]
                + r["kunden_neu"])

    top = sorted(rows, key=aktivitaet, reverse=True)[:12]
    betriebe = {
        "labels": [r["company_name"] for r in top],
        "data": [aktivitaet(r) for r in top],
    }
    return {"plattform": plattform, "betriebe": betriebe}


def _csv_val(v, fmt: str):
    """Rohwert fuers CSV (Zahlen statt formatierter Strings -> Excel-tauglich)."""
    if fmt == "eur":
        return f"{v:.2f}"
    if fmt == "cost":
        return f"{v:.4f}"
    return v


@router.get("/metrics", response_class=HTMLResponse)
async def metrics_view(
    request: Request,
    user: AdminUser = Depends(require_admin),
):
    days = _metrics_days(request)
    start = None if days == 0 else _today_utc_start() - dt.timedelta(days=days)
    tid = _metrics_tenant_id(request)

    async with get_session() as s:
        selected_tenant = None
        if tid is not None:
            selected_tenant = (await s.execute(
                select(Tenant).where(Tenant.id == tid)
            )).scalar_one_or_none()
        scope = tid if selected_tenant else None
        rows, totals = await _collect_metrics(s, start, tenant_id=scope)
        await audit(user_id=user.id, action="metrics.view",
                    request=request, session=s)

    tenant_q = f"&tenant={tid}" if selected_tenant else ""
    return templates.TemplateResponse(request, "metrics.html", {
        "request": request, "user": user,
        "rows": rows, "totals": totals, "tiles": _build_tiles(totals),
        "charts": _build_charts(rows, totals),
        "columns": _METRIC_COLUMNS, "days": days, "periods": _PERIODS,
        "metric_groups": _build_metric_groups(),
        "selected_tenant": selected_tenant, "tenant_q": tenant_q,
        "csrf_token": request.state.admin_csrf,
    })


@router.get("/metrics/export.csv")
async def metrics_export_csv(
    request: Request,
    user: AdminUser = Depends(require_admin),
):
    days = _metrics_days(request)
    start = None if days == 0 else _today_utc_start() - dt.timedelta(days=days)
    tid = _metrics_tenant_id(request)

    async with get_session() as s:
        selected_tenant = None
        if tid is not None:
            selected_tenant = (await s.execute(
                select(Tenant).where(Tenant.id == tid)
            )).scalar_one_or_none()
        scope = tid if selected_tenant else None
        rows, totals = await _collect_metrics(s, start, tenant_id=scope)
        await audit(user_id=user.id, action="metrics.export",
                    request=request, session=s)

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Betrieb", "Slug"] + [h for _k, h, _f in _METRIC_COLUMNS])
    for r in rows:
        w.writerow([r["company_name"], r["slug"]]
                   + [_csv_val(r[k], fmt) for k, _h, fmt in _METRIC_COLUMNS])
    w.writerow(["SUMME", ""]
               + [_csv_val(totals[k], fmt) for k, _h, fmt in _METRIC_COLUMNS])

    label = "gesamt" if days == 0 else f"{days}d"
    slug_part = f"_{selected_tenant.slug}" if selected_tenant else ""
    fname = f"metriken{slug_part}_{label}_{_today_utc_start().strftime('%Y%m%d')}.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
