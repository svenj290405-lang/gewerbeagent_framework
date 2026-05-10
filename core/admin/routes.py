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
        },
        "charts": {
            "timeline": {"labels": labels_30, "data": data_30},
            "providers": {"labels": prov_labels, "data": prov_data},
        },
        "usage": usage,
        "csrf_token": request.state.admin_csrf,
    })


# =====================================================================
# TENANT FEATURES (Paket-Toggle + Per-Feature-Override)
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

    Zeigt aktuelles Paket + Liste aller Catalog-Features mit Toggle-
    Buttons. Quick-Switch fuer Basis/Pro/Enterprise an der Spitze.
    """
    from core.features import (
        FEATURES, PACKAGES, ALL_PACKAGES, PACKAGE_CUSTOM,
        enabled_features_for_tenant, detect_package_from_features,
    )
    from core.features.catalog import PACKAGE_LABELS, features_in_package
    from core.models import ToolConfig

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
    detected = detect_package_from_features(enabled)

    # Feature-Liste fuers Template — sortiert nach Paket-Tier
    # (Basis-Features oben, Enterprise-Only unten)
    basis_keys = PACKAGES["basis"]
    pro_keys = PACKAGES["pro"] - basis_keys
    enterprise_keys = PACKAGES["enterprise"] - PACKAGES["pro"]

    def _section(keys, label):
        rows = []
        for f in sorted(FEATURES.values(), key=lambda x: x.label):
            if f.key in keys:
                rows.append({
                    "key": f.key,
                    "label": f.label,
                    "description": f.description,
                    "enabled": f.key in enabled,
                    "always_on": f.always_on,
                })
        return {"label": label, "rows": rows}

    sections = [
        _section(basis_keys, "Basis"),
        _section(pro_keys, "Pro"),
        _section(enterprise_keys, "Enterprise"),
    ]

    # always_on-Features als eigene "immer aktiv" Sektion
    always_on_rows = [
        {
            "key": f.key,
            "label": f.label,
            "description": f.description,
            "enabled": True,
            "always_on": True,
        }
        for f in sorted(FEATURES.values(), key=lambda x: x.label)
        if f.always_on
    ]
    sections.insert(0, {"label": "Immer aktiv", "rows": always_on_rows})

    return templates.TemplateResponse(request, "tenant_features.html", {
        "request": request, "user": user,
        "tenant": tenant,
        "sections": sections,
        "current_tier": tenant.package_tier,
        "detected_tier": detected,
        "is_drift": (
            tenant.package_tier != detected and detected != PACKAGE_CUSTOM
        ),
        "packages": [
            (pkg, PACKAGE_LABELS.get(pkg, pkg))
            for pkg in ALL_PACKAGES
        ],
        "csrf_token": request.state.admin_csrf,
    })


@router.post("/tenants/{tenant_id}/features/package")
async def tenant_features_apply_package(
    request: Request,
    tenant_id: str,
    package: str = Form(...),
    csrf_token: str = Form(...),
    user: AdminUser = Depends(require_admin),
):
    """Wendet ein vordefiniertes Paket auf den Tenant an.

    Setzt alle ToolConfig.enabled-Flags entsprechend dem Paket und
    aktualisiert tenant.package_tier. Idempotent.
    """
    require_csrf(request, csrf_token)
    from core.features import apply_package, ALL_PACKAGES

    if package not in ALL_PACKAGES:
        raise HTTPException(400, f"Unbekanntes Paket: {package}")

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

        old_tier = tenant.package_tier
        # apply_package macht eigene Session — wir comitten danach
        # die package_tier-Aenderung in dieser Session.

    # Paket anwenden (eigene Session intern)
    if package != "custom":
        await apply_package(tid, package)

    async with get_session() as s:
        tenant = (await s.execute(
            select(Tenant).where(Tenant.id == tid)
        )).scalar_one_or_none()
        tenant.package_tier = package
        await audit(
            user_id=user.id, action="tenant.features.package",
            target=tenant.slug, request=request, session=s,
            details={"old": old_tier, "new": package},
        )
        await s.commit()

    return RedirectResponse(
        f"/admin/tenants/{tenant_id}/features", status_code=303,
    )


@router.post("/tenants/{tenant_id}/features/{feature_key}/toggle")
async def tenant_features_toggle(
    request: Request,
    tenant_id: str,
    feature_key: str,
    csrf_token: str = Form(...),
    user: AdminUser = Depends(require_admin),
):
    """Togglet ein einzelnes Feature fuer den Tenant.

    Bei Abweichung vom vordefinierten Paket wird tenant.package_tier
    auf 'custom' gesetzt.
    """
    require_csrf(request, csrf_token)
    from core.features import (
        FEATURES, PACKAGE_CUSTOM,
        invalidate_feature_cache, detect_package_from_features,
        enabled_features_for_tenant,
    )
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
        await s.commit()

    invalidate_feature_cache(tid)

    # Nach Toggle: aktuelle Feature-Detection -> ggf. tier auf 'custom'
    enabled = await enabled_features_for_tenant(tid)
    detected = detect_package_from_features(enabled)

    async with get_session() as s:
        tenant = (await s.execute(
            select(Tenant).where(Tenant.id == tid)
        )).scalar_one_or_none()
        if detected == PACKAGE_CUSTOM and tenant.package_tier != PACKAGE_CUSTOM:
            tenant.package_tier = PACKAGE_CUSTOM
        elif detected != PACKAGE_CUSTOM and tenant.package_tier != detected:
            # Falls Sven nach Custom-Toggle wieder genau einem Paket
            # entspricht (z.B. eine Aenderung rueckgaengig gemacht),
            # auto-revert zu dem Paket
            tenant.package_tier = detected

        await audit(
            user_id=user.id, action="tenant.features.toggle",
            target=f"{tenant.slug}:{feature_key}", request=request, session=s,
            details={"old": old_value, "new": new_value},
        )
        await s.commit()

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
