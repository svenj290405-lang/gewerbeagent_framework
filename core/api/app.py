"""
FastAPI-Hauptanwendung fuer Gewerbeagent Framework.

Zentrale Webhook-Router unter /webhook/{tenant}/{plugin}/{endpoint}
- Laedt beim Start alle Plugins via discover_plugins()
- Dispatched Requests an den richtigen Plugin-Handler
- Sauberes Logging und Error-Handling
"""
from __future__ import annotations

import logging
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

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle-Hook: Plugins beim Start laden."""
    logger.info("Framework startet...")
    discover_plugins()
    logger.info(f"{len(PLUGIN_MANIFESTS)} Plugins geladen")
    yield
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

@app.get("/")
async def root() -> dict[str, Any]:
    """Health-Check + Infos."""
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

    # Dispatch an Plugin
    try:
        response_data = await plugin.on_webhook(endpoint, payload)
        return JSONResponse(content=response_data)
    except Exception as e:
        logger.exception(f"Plugin-Fehler in {plugin_name}/{endpoint}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Fehler im Plugin {plugin_name}: {str(e)}",
        )

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
) -> RedirectResponse:
    """
    Startet den OAuth-Flow: leitet Nutzer zu Google-Login weiter.
    """
    try:
        auth_url = generate_auth_url(tenant_slug=tenant, provider=provider)
        return RedirectResponse(url=auth_url, status_code=302)
    except Exception as e:
        logger.exception(f"OAuth-Start fehlgeschlagen: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/oauth/callback")
async def oauth_callback(
    code: str = Query(...),
    state: str = Query(...),
) -> HTMLResponse:
    """
    OAuth-Callback von Google.
    Tauscht code gegen Token, speichert verschluesselt in DB.
    """
    try:
        oauth_token = await handle_callback(code=code, state=state)
        return HTMLResponse(
            content=f"""
            <html>
            <body style="font-family: sans-serif; padding: 2em;">
                <h1>✅ Verknuepfung erfolgreich!</h1>
                <p>Google-Account <b>{oauth_token.account_email}</b> wurde mit
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
            content=f"""
            <html>
            <body style="font-family: sans-serif; padding: 2em;">
                <h1>❌ Verknuepfung fehlgeschlagen</h1>
                <p>Fehler: {str(e)}</p>
                <p>Bitte erneut versuchen oder an Sven wenden.</p>
            </body>
            </html>
            """,
            status_code=500,
        )
