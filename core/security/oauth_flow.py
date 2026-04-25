"""Google-OAuth-Flow mit persistenter State-Speicherung in DB.

Secrets kommen aus oauth_client_secret.json (nicht in Git).
"""
from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta, timezone

from google_auth_oauthlib.flow import Flow
from sqlalchemy import delete, select

from config.settings import settings
from core.database import AsyncSessionLocal
from core.models import OAuthState, OAuthToken, Tenant

logger = logging.getLogger(__name__)

# OAuth Scopes fuer Google Calendar
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]

STATE_LIFETIME_MINUTES = 30


def _load_client_config() -> dict:
    """Laedt oauth_client_secret.json aus dem Projekt-Root."""
    secrets_file = settings.project_root / "oauth_client_secret.json"
    if not secrets_file.exists():
        raise FileNotFoundError(
            f"OAuth-Client-Secrets nicht gefunden: {secrets_file}\n"
            f"Downloade die JSON von der Google Cloud Console und speichere "
            f"sie als oauth_client_secret.json im Framework-Root."
        )
    return json.loads(secrets_file.read_text(encoding="utf-8"))


def _get_redirect_uri() -> str:
    """Baut die Redirect-URI fuer OAuth-Callback."""
    return f"{settings.public_url.rstrip('/')}/oauth/callback"


async def generate_auth_url(tenant_slug: str, provider: str = "google") -> str:
    """Erzeugt OAuth-Autorisierungs-URL und persistiert State in DB."""
    if provider != "google":
        raise NotImplementedError(f"Provider {provider} noch nicht unterstuetzt")

    client_config = _load_client_config()
    flow = Flow.from_client_config(
        client_config,
        scopes=GOOGLE_SCOPES,
        redirect_uri=_get_redirect_uri(),
    )

    state = secrets.token_urlsafe(32)
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=state,
    )

    async with AsyncSessionLocal() as session:
        # Alte States aufraeumen
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=STATE_LIFETIME_MINUTES)
        await session.execute(delete(OAuthState).where(OAuthState.created_at < cutoff))

        oauth_state = OAuthState(
            state=state,
            tenant_slug=tenant_slug,
            provider=provider,
            code_verifier=flow.code_verifier or "",
        )
        session.add(oauth_state)
        await session.commit()

    logger.info(
        f"OAuth-URL generiert fuer Tenant {tenant_slug} (state={state[:8]}...)"
    )
    return auth_url


async def handle_callback(code: str, state: str) -> OAuthToken:
    """Verarbeitet OAuth-Callback: tauscht Code gegen Token, speichert in DB."""
    # State aus DB laden + loeschen
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(OAuthState).where(OAuthState.state == state)
        )
        oauth_state = result.scalar_one_or_none()
        if not oauth_state:
            raise ValueError(f"Unbekannter oder abgelaufener State: {state[:8]}...")

        age = datetime.now(timezone.utc) - oauth_state.created_at
        if age > timedelta(minutes=STATE_LIFETIME_MINUTES):
            await session.delete(oauth_state)
            await session.commit()
            raise ValueError("State abgelaufen")

        tenant_slug = oauth_state.tenant_slug
        provider = oauth_state.provider
        code_verifier = oauth_state.code_verifier
        await session.delete(oauth_state)
        await session.commit()

    # Token von Google holen
    client_config = _load_client_config()
    flow = Flow.from_client_config(
        client_config,
        scopes=GOOGLE_SCOPES,
        redirect_uri=_get_redirect_uri(),
        state=state,
    )
    if code_verifier:
        flow.code_verifier = code_verifier

    flow.fetch_token(code=code)
    creds = flow.credentials

    # User-Info holen
    import httpx
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {creds.token}"},
        )
        user_info = resp.json()
        account_email = user_info.get("email", "")

    # In DB speichern (upsert)
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Tenant).where(Tenant.slug == tenant_slug)
        )
        tenant = result.scalar_one_or_none()
        if not tenant:
            raise ValueError(f"Tenant {tenant_slug} nicht gefunden")

        result = await session.execute(
            select(OAuthToken).where(
                OAuthToken.tenant_id == tenant.id,
                OAuthToken.provider == provider,
            )
        )
        oauth_token = result.scalar_one_or_none()

        if oauth_token is None:
            oauth_token = OAuthToken(
                tenant_id=tenant.id,
                provider=provider,
            )
            session.add(oauth_token)

        oauth_token.refresh_token = creds.refresh_token or ""
        oauth_token.access_token = creds.token
        oauth_token.scopes = ",".join(creds.scopes) if creds.scopes else ""
        oauth_token.account_email = account_email
        if creds.expiry:
            oauth_token.access_token_expires_at = creds.expiry.replace(
                tzinfo=timezone.utc
            )

        await session.commit()
        await session.refresh(oauth_token)

        logger.info(
            f"OAuth-Token gespeichert: tenant={tenant_slug} account={account_email}"
        )
        return oauth_token
