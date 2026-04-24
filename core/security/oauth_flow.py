"""
Google-OAuth-Flow fuer Multi-Tenant-Setup.

Workflow:
1. generate_auth_url(tenant_slug, provider) -> URL zum Google-Login
2. User klickt URL, meldet sich bei Google an
3. Google redirected zu /oauth/callback?code=...&state=...
4. handle_callback(code, state) -> speichert Token verschluesselt in DB

Secrets kommen aus oauth_client_secret.json (nicht in Git).
"""
from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone

from google_auth_oauthlib.flow import Flow
from sqlalchemy import select

from config.settings import settings
from core.database import AsyncSessionLocal
from core.models import OAuthToken, Tenant

logger = logging.getLogger(__name__)

# Temporaerer State-Store (in Production: Redis oder DB)
# Mapping: state -> {tenant_slug, provider, code_verifier}
# code_verifier wird fuer PKCE benoetigt beim Token-Austausch
_STATE_STORE: dict[str, dict] = {}

# OAuth Scopes fuer Google Calendar
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]


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


def generate_auth_url(tenant_slug: str, provider: str = "google") -> str:
    """Generiert die Google-Login-URL fuer einen Tenant."""
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

    # State + PKCE-Verifier speichern
    _STATE_STORE[state] = {
        "tenant_slug": tenant_slug,
        "provider": provider,
        "code_verifier": flow.code_verifier,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    logger.info(f"OAuth-URL generiert fuer Tenant {tenant_slug}")
    return auth_url


async def handle_callback(code: str, state: str) -> OAuthToken:
    """Verarbeitet den OAuth-Callback von Google."""
    if state not in _STATE_STORE:
        raise ValueError(f"Unbekannter oder abgelaufener State: {state}")

    state_data = _STATE_STORE.pop(state)
    tenant_slug = state_data["tenant_slug"]
    provider = state_data["provider"]
    code_verifier = state_data.get("code_verifier")

    client_config = _load_client_config()
    flow = Flow.from_client_config(
        client_config,
        scopes=GOOGLE_SCOPES,
        redirect_uri=_get_redirect_uri(),
        state=state,
    )

    # PKCE-Verifier wieder anhaengen (wichtig!)
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
