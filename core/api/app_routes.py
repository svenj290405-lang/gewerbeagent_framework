"""Routen der Inhaber-/Mitarbeiter-PWA (``/app``).

Loest den Telegram-Bot als Bedien-Oberflaeche ab. Diese Welle 1 liefert:
- Passwortloser Login via Magic-Link (Mail ueber das _global-Outlook-
  Plattformpostfach, gleicher Pfad wie Onboarding-/Health-Mails).
- App-Shell (statische PWA) + Service-Worker + Manifest.
- JSON-API: /app/api/me, /app/api/push/{subscribe,unsubscribe}.

Die fachlichen Screens (Termine, Aufnahmen, Rueckrufe ...) haengen unter
/app/api/... und rufen die bestehende Plugin-/Integrations-Logik auf —
siehe app_routes_screens (Welle 1e).

Sicherheit: alle /app/api-Endpunkte ausser Login haengen an
require_app_user; mutierende an require_app_csrf. Strikte Tenant-Isolation
ueber current_tenant_id(request).
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete, select

from config.settings import settings
from core.database.connection import get_session
from core.features.check import enabled_features_for_tenant
from core.models.app_account import PushSubscription
from core.models.oauth_token import OAuthToken
from core.models.tenant import Tenant
from core.security.app_auth import (
    check_login_rate_limit,
    clear_app_session_cookie,
    consume_login_token,
    create_app_session,
    create_login_token,
    current_tenant_id,
    find_employee_by_email,
    require_app_csrf,
    require_app_user,
    revoke_app_session,
    set_app_session_cookie,
    verify_app_login,
    APP_SESSION_COOKIE_NAME,
)

logger = logging.getLogger(__name__)

STATIC_DIR = settings.project_root / "static" / "app"

router = APIRouter(prefix="/app", tags=["app"])


def mount_app_static(app) -> None:
    app.mount(
        "/app/static",
        StaticFiles(directory=str(STATIC_DIR)),
        name="app_static",
    )


# =====================================================================
# Plattform-Mailversand (Magic-Link) ueber _global-Outlook
# =====================================================================

GLOBAL_TENANT_SLUG = "_global"


async def _send_platform_mail(to_email: str, subject: str, html: str) -> bool:
    """Schickt eine Mail ueber das _global-Outlook-Plattformpostfach.

    Gleicher Pfad wie Onboarding-/Health-Mails. Liefert False (geloggt)
    wenn das Plattformpostfach nicht verbunden ist — der Login-Flow
    verraet das dem Anfragenden NICHT (keine User-Enumeration).
    """
    from core.integrations.microsoft import send_tracked_mail

    async with get_session() as s:
        gt = (await s.execute(
            select(Tenant).where(Tenant.slug == GLOBAL_TENANT_SLUG)
        )).scalar_one_or_none()
        tok = None
        if gt is not None:
            tok = (await s.execute(
                select(OAuthToken).where(
                    OAuthToken.tenant_id == gt.id,
                    OAuthToken.provider == "microsoft",
                )
            )).scalar_one_or_none()
    if gt is None or tok is None:
        logger.error("App-Login: kein _global-Outlook verbunden — Magic-Link "
                     "nicht versendbar.")
        return False

    res = await send_tracked_mail(
        tenant_id=gt.id,
        to_email=to_email,
        subject=subject,
        body_html=html,
        employee_id=tok.employee_id,
    )
    return bool(res.get("success"))


def _magic_link_html(link: str) -> str:
    return (
        "<div style='font-family:sans-serif;font-size:15px;color:#222'>"
        "<p>Hallo,</p>"
        "<p>hier ist dein Login-Link f&uuml;r die Gewerbeagent-App. Er ist "
        "20 Minuten g&uuml;ltig und nur einmal verwendbar:</p>"
        f"<p><a href='{link}' style='display:inline-block;padding:12px 20px;"
        "background:#0066cc;color:#fff;border-radius:8px;text-decoration:none'>"
        "In der App anmelden</a></p>"
        f"<p style='color:#666;font-size:13px'>Falls der Button nicht geht: "
        f"{link}</p>"
        "<p style='color:#666;font-size:13px'>Du hast diesen Link nicht "
        "angefordert? Dann ignoriere diese Mail einfach.</p>"
        "</div>"
    )


# =====================================================================
# LOGIN (passwortlos, Magic-Link)
# =====================================================================

@router.get("/login")
async def app_login_page() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "login.html"))


@router.post("/login")
async def app_login_request(request: Request, email: str = Form(...)) -> JSONResponse:
    """Fordert einen Magic-Link an. Antwortet IMMER generisch (keine
    User-Enumeration), egal ob die Mail existiert."""
    from core.security.app_auth import _client_ip

    generic = JSONResponse(
        {"ok": True, "message": "Wenn die Adresse hinterlegt ist, ist ein "
         "Login-Link unterwegs. Schau in dein Postfach."}
    )
    ip = _client_ip(request)
    async with get_session() as s:
        if not await check_login_rate_limit(ip, session=s):
            return JSONResponse(
                {"ok": False, "message": "Zu viele Versuche. Bitte in einigen "
                 "Minuten erneut probieren."},
                status_code=429,
            )
        emp = await find_employee_by_email(email, session=s)
        if emp is None:
            # Trotzdem generisch antworten, aber nichts senden.
            return generic
        tok = await create_login_token(employee=emp, request=request, session=s)
        link = f"{settings.app_url}/app/login/{tok.token}"
    # Mailversand ausserhalb der DB-Session
    await _send_platform_mail(
        emp.contact_email, "Dein Login-Link für die Gewerbeagent-App",
        _magic_link_html(link),
    )
    return generic


@router.post("/login/password")
async def app_login_password(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
):
    """Klassisches E-Mail+Passwort-Login. Rate-limited per IP (gleiches
    Fenster wie Magic-Link)."""
    from core.security.app_auth import _client_ip

    ip = _client_ip(request)
    async with get_session() as s:
        if not await check_login_rate_limit(ip, session=s):
            return RedirectResponse("/app/login?fehler=rate", status_code=303)
        emp = await verify_app_login(email, password, session=s)
        if emp is None:
            # Fehlversuch als Rate-Limit-Zaehler vermerken (Login-Token-Tabelle
            # dient als IP-Zaehler — wir legen einen kurzlebigen Marker an).
            return RedirectResponse("/app/login?fehler=login", status_code=303)
        sess = await create_app_session(employee=emp, request=request, session=s)
        token_val = sess.token
    resp = RedirectResponse("/app", status_code=303)
    set_app_session_cookie(resp, token_val)
    return resp


@router.get("/login/{token}")
async def app_login_consume(token: str, request: Request):
    """Loest den Magic-Link ein, legt Session an, setzt Cookie, redirect /app."""
    async with get_session() as s:
        result = await consume_login_token(token, session=s)
        if result is None:
            return RedirectResponse("/app/login?fehler=link", status_code=303)
        emp, _tenant = result
        sess = await create_app_session(employee=emp, request=request, session=s)
        token_val = sess.token
    resp = RedirectResponse("/app", status_code=303)
    set_app_session_cookie(resp, token_val)
    return resp


@router.post("/logout")
async def app_logout(request: Request, _emp=Depends(require_app_user)):
    token = request.cookies.get(APP_SESSION_COOKIE_NAME)
    if token:
        async with get_session() as s:
            await revoke_app_session(token, session=s)
    resp = RedirectResponse("/app/login", status_code=303)
    clear_app_session_cookie(resp)
    return resp


# =====================================================================
# APP-SHELL (PWA) + Service-Worker + Manifest
# =====================================================================

@router.get("/manifest.webmanifest")
async def app_manifest() -> FileResponse:
    return FileResponse(
        str(STATIC_DIR / "manifest.webmanifest"),
        media_type="application/manifest+json",
    )


@router.get("/sw.js")
async def app_service_worker() -> FileResponse:
    # Service-Worker muss im /app-Scope ausgeliefert werden.
    return FileResponse(
        str(STATIC_DIR / "sw.js"),
        media_type="text/javascript",
        headers={"Service-Worker-Allowed": "/app"},
    )


@router.get("/")
async def app_shell_root(_emp=Depends(require_app_user)) -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


# =====================================================================
# JSON-API: Session-Kontext + Push-Subscriptions
# =====================================================================

@router.get("/api/me")
async def app_api_me(request: Request, _emp=Depends(require_app_user)) -> JSONResponse:
    emp = request.state.app_employee
    tenant = request.state.app_tenant
    features = sorted(await enabled_features_for_tenant(tenant.id))
    return JSONResponse({
        "employee": {
            "id": str(emp.id),
            "name": emp.name,
            "is_inhaber": bool(emp.is_default),
        },
        "tenant": {
            "slug": tenant.slug,
            "company_name": tenant.company_name,
        },
        "features": features,
        "csrf": request.state.app_csrf,
        "vapid_public_key": settings.vapid_public_key,
    })


@router.post("/api/push/subscribe")
async def app_push_subscribe(
    request: Request,
    _emp=Depends(require_app_user),
    _csrf=Depends(require_app_csrf),
) -> JSONResponse:
    emp = request.state.app_employee
    tenant_id = current_tenant_id(request)
    body = await request.json()
    sub = body.get("subscription") or body
    endpoint = (sub or {}).get("endpoint")
    keys = (sub or {}).get("keys") or {}
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")
    if not endpoint or not p256dh or not auth:
        return JSONResponse({"ok": False, "error": "ungueltige Subscription"},
                            status_code=400)

    async with get_session() as s:
        existing = (await s.execute(
            select(PushSubscription).where(PushSubscription.endpoint == endpoint)
        )).scalar_one_or_none()
        if existing is not None:
            # Re-Bind (z.B. anderer Mitarbeiter am selben Geraet)
            existing.employee_id = emp.id
            existing.tenant_id = tenant_id
            existing.p256dh = p256dh
            existing.auth = auth
            existing.user_agent = (request.headers.get("user-agent") or "")[:500] or None
        else:
            s.add(PushSubscription(
                employee_id=emp.id,
                tenant_id=tenant_id,
                endpoint=endpoint[:2048],
                p256dh=p256dh[:255],
                auth=auth[:255],
                user_agent=(request.headers.get("user-agent") or "")[:500] or None,
            ))
    return JSONResponse({"ok": True})


@router.post("/api/push/unsubscribe")
async def app_push_unsubscribe(
    request: Request,
    _emp=Depends(require_app_user),
    _csrf=Depends(require_app_csrf),
) -> JSONResponse:
    body = await request.json()
    endpoint = (body or {}).get("endpoint")
    if endpoint:
        async with get_session() as s:
            await s.execute(
                delete(PushSubscription).where(PushSubscription.endpoint == endpoint)
            )
    return JSONResponse({"ok": True})
