"""
Google-OAuth-Service fuer Kalender-Plugin.

Laedt verschluesselte OAuth-Tokens aus der DB, refresht wenn noetig,
und gibt einen authentifizierten Calendar-Service zurueck.
"""
from __future__ import annotations

import uuid
from datetime import timezone

from google.auth.transport.requests import Request as GRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.models import OAuthToken
from core.security.oauth_flow import _load_client_config


def _get_google_client_creds() -> tuple[str, str]:
    """Holt client_id und client_secret aus oauth_client_secret.json."""
    cfg = _load_client_config()
    web_cfg = cfg.get("web") or cfg.get("installed") or {}
    return web_cfg.get("client_id", ""), web_cfg.get("client_secret", "")


async def get_calendar_service(
    tenant_id: uuid.UUID,
    employee_id: uuid.UUID | None = None,
):
    """
    Gibt authentifizierten Google Calendar Service zurueck.

    Phase 1 Multi-OAuth: optional employee_id — nutzt zentralen
    Lookup-Helper mit 3-stufigem Fallback (employee → default-emp →
    legacy-tenant-token).

    Raises:
        ValueError: wenn kein Google-OAuth-Token zu finden ist
    """
    from core.security.oauth_token_lookup import find_oauth_token

    oauth_token = await find_oauth_token(tenant_id, "google", employee_id)
    if not oauth_token:
        scope = f"emp={employee_id}" if employee_id else f"tenant={tenant_id}"
        raise ValueError(
            f"Kein Google-OAuth-Token fuer {scope} in der DB"
        )

    # Token-Refresh: in der gleichen Session weitermachen damit der
    # commit() den access_token zurueckschreibt
    async with AsyncSessionLocal() as session:
        # Re-fetch in dieser Session damit ORM den Token tracked
        result = await session.execute(
            select(OAuthToken).where(OAuthToken.id == oauth_token.id)
        )
        oauth_token = result.scalar_one()

        client_id, client_secret = _get_google_client_creds()

        creds = Credentials(
            token=oauth_token.access_token,
            refresh_token=oauth_token.refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=oauth_token.scopes.split(",") if oauth_token.scopes else [],
        )

        # Access-Token abgelaufen? Refreshen und speichern
        if not creds.valid and creds.refresh_token:
            creds.refresh(GRequest())
            oauth_token.access_token = creds.token
            if creds.expiry:
                oauth_token.access_token_expires_at = creds.expiry.replace(
                    tzinfo=timezone.utc
                )
            await session.commit()

    return build("calendar", "v3", credentials=creds)
