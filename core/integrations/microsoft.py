"""Microsoft Graph API Integration: Mail senden im Namen des Tenants.

Nutzt OAuth-Tokens aus oauth_tokens-Tabelle, refreshed automatisch wenn abgelaufen.

Hauptfunktionen:
- get_microsoft_token(tenant_id) -> str: liefert gueltigen access_token
- send_mail_as_user(tenant_id, ...): schickt Mail via Graph API
- get_microsoft_status(tenant_id): zeigt Verbindungsstatus
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

import httpx
from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.models import OAuthToken
from core.security.oauth_flow import (
    MICROSOFT_TOKEN_URL,
    MICROSOFT_SCOPES,
    _load_microsoft_config,
)

logger = logging.getLogger(__name__)

GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"


class MicrosoftNotConnectedError(Exception):
    """Tenant hat Microsoft-Account nicht verbunden."""
    pass


async def _refresh_access_token(oauth_token: OAuthToken) -> tuple[str, datetime]:
    """Holt neuen access_token via refresh_token.

    Returns: (new_access_token, expires_at)
    """
    cfg = await _load_microsoft_config()

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            MICROSOFT_TOKEN_URL,
            data={
                "client_id": cfg["client_id"],
                "client_secret": cfg["client_secret"],
                "refresh_token": oauth_token.refresh_token,
                "grant_type": "refresh_token",
                "scope": " ".join(MICROSOFT_SCOPES),
            },
            headers={"Accept": "application/json"},
        )
        if resp.status_code != 200:
            raise ValueError(
                f"Microsoft Token-Refresh fehlgeschlagen: "
                f"{resp.status_code} {resp.text[:300]}"
            )
        tokens = resp.json()

    new_access = tokens.get("access_token")
    new_refresh = tokens.get("refresh_token")
    expires_in = int(tokens.get("expires_in", 3600))

    if not new_access:
        raise ValueError("Microsoft hat keinen access_token zurueckgegeben")

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in - 60)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(OAuthToken).where(OAuthToken.id == oauth_token.id)
        )
        token_db = result.scalar_one()
        token_db.access_token = new_access
        token_db.access_token_expires_at = expires_at
        if new_refresh:
            token_db.refresh_token = new_refresh
        await session.commit()

    logger.info(
        f"Microsoft access_token erneuert fuer tenant_id={oauth_token.tenant_id}"
    )
    return new_access, expires_at


async def get_microsoft_token(tenant_id: UUID) -> str:
    """Liefert gueltigen access_token fuer Tenant. Refreshed wenn noetig.

    Raises MicrosoftNotConnectedError wenn Tenant nicht verbunden.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(OAuthToken).where(
                OAuthToken.tenant_id == tenant_id,
                OAuthToken.provider == "microsoft",
            )
        )
        oauth_token = result.scalar_one_or_none()

    if not oauth_token:
        raise MicrosoftNotConnectedError(
            "Microsoft-Account nicht verbunden. Tenant soll /microsoft_setup nutzen."
        )

    now = datetime.now(timezone.utc)
    expires = oauth_token.access_token_expires_at
    if expires and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)

    if oauth_token.access_token and expires and expires > now:
        return oauth_token.access_token

    new_token, _ = await _refresh_access_token(oauth_token)
    return new_token


async def send_mail_as_user(
    tenant_id: UUID,
    to_email: str,
    subject: str,
    body_html: str,
    cc: Optional[list[str]] = None,
    save_to_sent: bool = True,
) -> bool:
    """Sendet Mail im Namen des verbundenen Microsoft-Users via Graph API.

    Returns: True bei Erfolg, False bei Fehler.
    """
    try:
        access_token = await get_microsoft_token(tenant_id)
    except MicrosoftNotConnectedError:
        logger.warning(f"send_mail_as_user: Tenant {tenant_id} nicht mit Microsoft verbunden")
        return False
    except Exception as e:
        logger.error(f"send_mail_as_user Token-Fehler: {e}")
        return False

    message = {
        "message": {
            "subject": subject,
            "body": {
                "contentType": "HTML",
                "content": body_html,
            },
            "toRecipients": [
                {"emailAddress": {"address": to_email}}
            ],
        },
        "saveToSentItems": save_to_sent,
    }

    if cc:
        message["message"]["ccRecipients"] = [
            {"emailAddress": {"address": addr}} for addr in cc
        ]

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"{GRAPH_API_BASE}/me/sendMail",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json=message,
            )
            if resp.status_code in (200, 202):
                logger.info(
                    f"Microsoft-Mail gesendet: tenant={tenant_id} to={to_email} "
                    f"subject={subject[:50]!r}"
                )
                return True
            logger.error(
                f"Microsoft sendMail fehlgeschlagen: "
                f"{resp.status_code} {resp.text[:300]}"
            )
            return False
    except Exception as e:
        logger.exception(f"send_mail_as_user Exception: {e}")
        return False


async def get_microsoft_status(tenant_id: UUID) -> dict:
    """Zeigt Verbindungs-Status fuer Tenant.

    Returns: {connected, account_email, expires_at, scopes}
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(OAuthToken).where(
                OAuthToken.tenant_id == tenant_id,
                OAuthToken.provider == "microsoft",
            )
        )
        oauth_token = result.scalar_one_or_none()

    if not oauth_token:
        return {
            "connected": False,
            "account_email": None,
            "expires_at": None,
            "scopes": None,
        }

    return {
        "connected": True,
        "account_email": oauth_token.account_email,
        "expires_at": oauth_token.access_token_expires_at,
        "scopes": oauth_token.scopes,
    }
