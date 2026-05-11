"""
FastAPI-Hauptanwendung fuer Gewerbeagent Framework.

Zentrale Webhook-Router unter /webhook/{tenant}/{plugin}/{endpoint}
- Laedt beim Start alle Plugins via discover_plugins()
- Dispatched Requests an den richtigen Plugin-Handler
- Sauberes Logging und Error-Handling
"""
from __future__ import annotations

import logging
import asyncio
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from config.settings import settings
from core.plugin_system import (
    PLUGIN_MANIFESTS,
    discover_plugins,
    get_plugin_for_tenant,
)
from core.api.anfrage_routes import router as anfrage_router
from core.admin.routes import router as admin_router, mount_static as mount_admin_static
from core.integrations.microsoft_cron import cron_loop as microsoft_cron_loop
from core.integrations.rechnung_payment_monitor import (
    cron_loop as rechnung_payment_cron_loop,
)
from core.integrations.rechnung_paid_summary import (
    cron_loop as rechnung_paid_summary_cron_loop,
)
from core.integrations.dsgvo_cleanup_cron import cron_loop as dsgvo_cleanup_cron_loop
from core.integrations.mail_retry_cron import cron_loop as mail_retry_cron_loop
from core.integrations.db_maintenance_cron import cron_loop as db_maintenance_cron_loop

logger = logging.getLogger(__name__)
# Phase B1: strukturiertes Logging mit Tenant-/Employee-Context.
# Ersetzt das frueher genutzte basicConfig — der Filter haengt automatisch
# tenant + employee an jede Zeile, gefuettert via core.logging_context.
from core.logging_context import configure_structured_logging
configure_structured_logging(level=settings.log_level)

# Phase B2: Sentry init (opt-in via SENTRY_DSN env). No-op wenn nicht
# konfiguriert — kein Boot-Fail moeglich.
from core.integrations.error_tracking import init_sentry
init_sentry()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle-Hook: Plugins beim Start laden + Cron-Tasks starten."""
    logger.info("Framework startet...")
    discover_plugins()
    logger.info(f"{len(PLUGIN_MANIFESTS)} Plugins geladen")

    # Cron-Trennung Dev/Prod: im Dev-Stack mit DEV_CRON_DISABLED=true
    # werden die Background-Loops uebersprungen → kein API-Quota-
    # Verbrauch, keine Test-Mails, keine Bezahl-Polls fuer Prod-Tenants.
    if settings.crons_enabled:
        cron_task = asyncio.create_task(microsoft_cron_loop())
        logger.info("Microsoft-Cron als Background-Task gestartet")

        payment_task = asyncio.create_task(rechnung_payment_cron_loop())
        logger.info("Bezahl-Polling-Cron (Lexware, alle 30 Min) gestartet")

        summary_task = asyncio.create_task(rechnung_paid_summary_cron_loop())
        logger.info("Bezahl-Tages-Zusammenfassung-Cron (taegl. 18:00) gestartet")

        dsgvo_task = asyncio.create_task(dsgvo_cleanup_cron_loop())
        logger.info("DSGVO-Cleanup-Cron (taegl. 03:00) gestartet")

        mail_retry_task = asyncio.create_task(mail_retry_cron_loop())
        logger.info("Mail-Retry-Cron (alle 5 Min) gestartet")

        db_maintenance_task = asyncio.create_task(db_maintenance_cron_loop())
        logger.info("DB-Maintenance-Cron (taegl. 02:00) gestartet")

        cron_tasks = (
            cron_task, payment_task, summary_task, dsgvo_task,
            mail_retry_task, db_maintenance_task,
        )
    else:
        logger.warning(
            "Cron-Loops deaktiviert (environment=%s, dev_cron_disabled=%s)",
            settings.environment, settings.dev_cron_disabled,
        )
        cron_tasks = ()

    yield

    for t in cron_tasks:
        t.cancel()
    for t in cron_tasks:
        try:
            await t
        except asyncio.CancelledError:
            pass

    # Shutdown-Hook: alle Rechnungen die noch in Status 'creating' haengen
    # auf 'error' setzen. Sonst kann der Tenant beim naechsten Restart
    # ewig auf die "wird erstellt..."-Meldung warten. Failsafe — keine
    # Exception darf den Shutdown stoeren.
    try:
        from core.integrations.rechnung_payment_monitor import (
            cleanup_stale_creating_rechnungen,
        )
        # stale_minutes=0 = ALLE creating-Rechnungen sofort auf error.
        recovered = await cleanup_stale_creating_rechnungen(stale_minutes=0)
        if recovered > 0:
            logger.warning(
                "Shutdown-Hook: %d 'creating'-Rechnungen auf 'error' "
                "gesetzt (Container-Restart)", recovered,
            )
    except Exception:
        # Beta-1 B1-8: Stack-Trace ist wertvoll bei Shutdown-Crashes
        # weil schwer reproduzierbar — logger.exception statt warning.
        logger.exception("Shutdown-Hook (creating-Cleanup) fehler")

    logger.info("Framework faehrt runter.")


app = FastAPI(
    title="Gewerbeagent Framework",
    description="Multi-Tenant SaaS fuer Handwerksbetriebe",
    version="0.1.0",
    lifespan=lifespan,
)


# ============================================================
# ROUTES
# ============================================================

app.include_router(anfrage_router)
app.include_router(admin_router)
mount_admin_static(app)

# Phase B6: Status-Page Routes (/status + /api/status). Oeffentlich
# erreichbar — Caddy proxy auch von status.gewerbeagent.de hierher.
from core.api.status_routes import router as status_router
app.include_router(status_router)


# Admin-Redirect-Exception in echten 303-Redirect umwandeln
from core.admin.auth import _AdminRedirect

@app.exception_handler(_AdminRedirect)
async def _admin_redirect_handler(request: Request, exc: _AdminRedirect):
    from fastapi.responses import RedirectResponse
    target = exc.headers.get("Location", "/admin/login") if exc.headers else "/admin/login"
    return RedirectResponse(target, status_code=303)


@app.get("/")
async def root() -> dict[str, Any]:
    """Minimaler Health-Check.

    Hardening: in Production keine Plugin-Liste, Version oder Env
    ausgeben — gibt Angreifern unnoetige Recon-Infos. In Development
    (z.B. lokal) bleibt es ausfuehrlich fuer Debug-Komfort.
    """
    if settings.is_production:
        return {"status": "ok"}
    return {
        "status": "ok",
        "service": "Gewerbeagent Framework",
        "version": "0.1.0",
        "environment": settings.environment,
        "plugins_loaded": list(PLUGIN_MANIFESTS.keys()),
    }


@app.get("/health")
async def health() -> dict[str, str]:
    """Simple Liveness-Check fuer Monitoring."""
    return {"status": "healthy"}


@app.post("/webhook/{tenant_slug}/{plugin_name}/{endpoint}")
async def webhook_dispatch(
    tenant_slug: str,
    plugin_name: str,
    endpoint: str,
    request: Request,
) -> JSONResponse:
    """
    Zentraler Webhook-Dispatcher.

    Laedt Plugin fuer Tenant, delegiert Request, returniert Plugin-Response.
    """
    # Request-Body parsen (darf leer sein)
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    logger.info(
        f"Webhook: tenant={tenant_slug} plugin={plugin_name} endpoint={endpoint}"
    )

    # B1: Tenant-Context fuer alle nachfolgenden Logs in diesem Request
    from core.logging_context import set_log_tenant
    set_log_tenant(tenant_slug)  # slug ist genauso gut wie UUID hier

    # Plugin fuer diesen Tenant holen
    plugin = await get_plugin_for_tenant(tenant_slug, plugin_name)

    if plugin is None:
        logger.warning(
            f"Plugin nicht verfuegbar: tenant={tenant_slug} plugin={plugin_name}"
        )
        raise HTTPException(
            status_code=404,
            detail=(
                f"Plugin '{plugin_name}' fuer Tenant '{tenant_slug}' "
                f"nicht verfuegbar. Pruefe ob der Tenant existiert und das "
                f"Plugin aktiviert ist."
            ),
        )

    # Header-Dict zum Plugin durchreichen (lowercase fuer konsistenten
    # Lookup von Signatur-Headers wie X-Telegram-Bot-Api-Secret-Token).
    headers_lc = {k.lower(): v for k, v in request.headers.items()}

    # Dispatch an Plugin
    try:
        response_data = await plugin.on_webhook(endpoint, payload, headers=headers_lc)
        return JSONResponse(content=response_data)
    except PermissionError as e:
        # Plugin hat Signatur abgelehnt — generische 401, keine Detail-Leaks
        logger.warning(
            f"Webhook abgelehnt: tenant={tenant_slug} plugin={plugin_name} "
            f"endpoint={endpoint} reason={e}"
        )
        raise HTTPException(status_code=401, detail="Unauthorized")
    except Exception as e:
        logger.exception(f"Plugin-Fehler in {plugin_name}/{endpoint}: {e}")
        # Production: keine Detail-Leaks an Caller (Stack-Trace bleibt im Log).
        # Dev: voller Error-String fuer schnelleres Debugging.
        if settings.is_production:
            detail = "Plugin-Fehler. Details im Server-Log."
        else:
            detail = f"Fehler im Plugin {plugin_name}: {str(e)}"
        raise HTTPException(status_code=500, detail=detail)

# ============================================================
# OAUTH ENDPOINTS
# ============================================================

from fastapi import Query
from fastapi.responses import RedirectResponse, HTMLResponse

from core.security.oauth_flow import generate_auth_url, handle_callback


@app.get("/oauth/start")
async def oauth_start(
    tenant: str = Query(..., description="Tenant-Slug, z.B. 'dietz'"),
    provider: str = Query("google", description="OAuth-Provider"),
    employee: str | None = Query(
        None,
        description="Optionaler Mitarbeiter-Slug (Phase 1 Multi-OAuth). "
                    "Ohne diesen Param landet der Token beim Default-Employee.",
    ),
) -> RedirectResponse:
    """
    Startet den OAuth-Flow: leitet Nutzer zu Google/Microsoft-Login weiter.

    Hardening: Tenant- und Employee-Slug-Format wird strikt validiert
    bevor sie an die OAuth-Funktion gehen — keine sonderzeichen-basierten
    Bypasses.
    """
    import re
    if not re.fullmatch(r"[a-z0-9_-]{1,50}", tenant):
        raise HTTPException(status_code=400, detail="Ungueltiger Tenant-Slug")
    if employee is not None and not re.fullmatch(r"[a-z0-9_-]{1,64}", employee):
        raise HTTPException(status_code=400, detail="Ungueltiger Employee-Slug")
    if provider not in ("google", "microsoft"):
        raise HTTPException(status_code=400, detail="Unbekannter OAuth-Provider")
    try:
        auth_url = await generate_auth_url(
            tenant_slug=tenant, provider=provider, employee_slug=employee,
        )
        return RedirectResponse(url=auth_url, status_code=302)
    except Exception as e:
        # Internal-Fehler nicht ans Frontend leaken (kein str(e))
        logger.exception(f"OAuth-Start fehlgeschlagen: {e}")
        raise HTTPException(status_code=500, detail="OAuth-Start fehlgeschlagen")


@app.get("/oauth/callback")
async def oauth_callback(
    code: str = Query(...),
    state: str = Query(...),
) -> HTMLResponse:
    """
    OAuth-Callback von Google.
    Tauscht code gegen Token, speichert verschluesselt in DB.

    Hardening: HTML-escaped Render der erfolgreichen account_email +
    generische Fehler-Seite ohne Exception-Details (verhindert Recon
    via OAuth-Fehler-Messages, z.B. 'tenant XYZ not found').
    """
    from html import escape as _h
    try:
        oauth_token = await handle_callback(code=code, state=state)
        safe_email = _h(oauth_token.account_email or "?")
        return HTMLResponse(
            content=f"""
            <html>
            <body style="font-family: sans-serif; padding: 2em;">
                <h1>✅ Verknuepfung erfolgreich!</h1>
                <p>Account <b>{safe_email}</b> wurde mit
                dem Gewerbeagent-Framework verknuepft.</p>
                <p>Du kannst dieses Fenster jetzt schliessen.</p>
            </body>
            </html>
            """,
            status_code=200,
        )
    except Exception as e:
        logger.exception(f"OAuth-Callback fehlgeschlagen: {e}")
        return HTMLResponse(
            content="""
            <html>
            <body style="font-family: sans-serif; padding: 2em;">
                <h1>❌ Verknuepfung fehlgeschlagen</h1>
                <p>Bitte erneut versuchen. Sollte das Problem bestehen
                bleiben, wende dich an den Support
                (hallo@gewerbeagent.de). Details findest du im Server-Log.</p>
            </body>
            </html>
            """,
            status_code=500,
        )
