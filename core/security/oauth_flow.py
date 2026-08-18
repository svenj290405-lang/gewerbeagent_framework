"""Google-OAuth-Flow mit persistenter State-Speicherung in DB.

Secrets kommen aus oauth_client_secret.json (nicht in Git).
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone

import httpx
from google_auth_oauthlib.flow import Flow
from sqlalchemy import delete, select

from config.settings import settings
from core.database import AsyncSessionLocal
from core.models import OAuthState, OAuthToken, Tenant

logger = logging.getLogger(__name__)

# OAuth Scopes fuer Google Calendar + Drive
# drive.file: nur Q-erstellte Ordner sichtbar (Privacy-by-Design).
# Bestehende Tokens haben den drive.file-Scope NICHT — Tenant muss
# einmal /drive_verbinden machen damit Drive-Funktionen greifen.
# Calendar bleibt davon unbeeinflusst.
# =====================================================================
# Scope-Profile
# =====================================================================
#
# Der Inhaber verbindet das Betriebskonto und braucht vollen Zugriff
# (Kalender lesen/schreiben fuer die Umverteilung bei Krankheit, Drive
# fuer das Kundenarchiv).
#
# Ein MITARBEITER verbindet sein privates Konto. Dort waere voller
# Kalenderzugriff nicht nur unnoetig, sondern eine Zusage, die wir
# technisch nicht halten koennen: "wir lesen nichts Privates" haengt
# sonst allein an unserer Disziplin. Deshalb ein enges Profil:
#
#   calendar.freebusy      -> nur "wann ist er belegt", keine Titel,
#                             keine Teilnehmer, keine Orte
#   calendar.app.created   -> ein von UNS angelegter Zweitkalender,
#                             in dem die Auftraege stehen
#
# Wichtig: calendar.app.created gibt KEINEN Zugriff auf den
# Hauptkalender. Q kann dort also gar nicht schreiben — die Termine
# landen in einem eigenen Kalender "<Betrieb> – Auftraege" im Konto des
# Mitarbeiters. Privat und dienstlich sind damit auch fuer ihn sichtbar
# getrennt. Drive gibt es fuer Mitarbeiter gar nicht.
SCOPE_PROFIL_VOLL = "voll"
SCOPE_PROFIL_MITARBEITER = "mitarbeiter"

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]

GOOGLE_SCOPES_MITARBEITER = [
    "https://www.googleapis.com/auth/calendar.app.created",
    "https://www.googleapis.com/auth/calendar.freebusy",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]


def google_scopes(scope_profil: str | None) -> list[str]:
    """Scope-Liste fuer ein Profil. Unbekannt/None -> voll (Bestand)."""
    if scope_profil == SCOPE_PROFIL_MITARBEITER:
        return list(GOOGLE_SCOPES_MITARBEITER)
    return list(GOOGLE_SCOPES)

# State-TTL: zwischen /oauth/start (Klick) und /oauth/callback (Login
# zurück) liegen Sekunden bis maximal 1-2 Minuten. 10 Min ist großzügig
# fuer langsame Logins ueber Recovery-Faktoren. Frueher waren es 30 Min
# — runtergezogen damit gestohlene OAuth-Links nicht so lange gueltig
# bleiben.
STATE_LIFETIME_MINUTES = 10

# =====================================================================
# Microsoft OAuth Configuration (Azure App Registration)
# =====================================================================
MICROSOFT_AUTHORIZE_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
MICROSOFT_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
MICROSOFT_USERINFO_URL = "https://graph.microsoft.com/v1.0/me"
MICROSOFT_SCOPES = [
    "Mail.Send",
    "Mail.ReadWrite",
    "User.Read",
    "offline_access",
    # Outlook-Calendar (analog Google Calendar): lesen+schreiben+loeschen
    # plus FreeBusy-Check fuer Slot-Suche
    "Calendars.ReadWrite",
    # Mailbox-Timezone (read-only) — wir warnen beim Onboarding wenn
    # die Outlook-Mailbox auf UTC steht, weil sonst Termine 2h
    # verschoben angezeigt werden (siehe _handle_callback_microsoft).
    "MailboxSettings.Read",
]

# Mitarbeiter-Profil fuer Outlook.
#
# EHRLICHE EINSCHRAENKUNG: Microsoft Graph kennt keine Entsprechung zu
# Googles calendar.app.created. Zum Anlegen von Terminen braucht es
# Calendars.ReadWrite, und das ist Vollzugriff auf den Kalender. Die
# technisch erzwungene Zusage "wir sehen nichts Privates" ist auf Google
# einloesbar, auf Outlook nicht — das gehoert so in die Verbinden-UI,
# sonst ist die Zusage falsch.
#
# Was hier trotzdem entfaellt und wichtiger ist: JEDER Mail-Scope. Ein
# Monteur, der bisher seinen Kalender verbunden haette, haette dem
# Betrieb sein komplettes Postfach mitgegeben (Mail.ReadWrite).
MICROSOFT_SCOPES_MITARBEITER = [
    "User.Read",
    "offline_access",
    "Calendars.ReadWrite",
    "MailboxSettings.Read",
]


def microsoft_scopes(scope_profil: str | None) -> list[str]:
    """Scope-Liste fuer ein Profil. Unbekannt/None -> voll (Bestand)."""
    if scope_profil == SCOPE_PROFIL_MITARBEITER:
        return list(MICROSOFT_SCOPES_MITARBEITER)
    return list(MICROSOFT_SCOPES)


async def _load_microsoft_config() -> dict:
    """Laedt Microsoft OAuth Config aus tool_configs._global."""
    from core.models import ToolConfig

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ToolConfig).where(ToolConfig.tool_name == "microsoft_oauth")
        )
        cfg = result.scalar_one_or_none()
        if not cfg or not cfg.config:
            raise ValueError(
                "Microsoft OAuth nicht konfiguriert. "
                "Bitte tool_configs._global.microsoft_oauth setzen."
            )
        return cfg.config


def _generate_pkce_pair() -> tuple[str, str]:
    """Erzeugt PKCE code_verifier + code_challenge (S256).

    Returns: (verifier, challenge)
    """
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


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


# State-Praefix kodiert, ob dieser Flow eine bestehende, bereits verbundene
# Kalender-/Mail-Anbindung auf ein ANDERES Konto umbiegen darf. Nur der
# authentifizierte PWA-Pfad (require_app_inhaber) setzt "r"; der oeffentliche
# GET /oauth/start (Telegram-Deeplinks, Reauth-Mails) bleibt "n" und kann so
# eine verbundene Anbindung nicht fremduebernehmen (Confused-Deputy-Schutz).
_STATE_PREFIX_REBIND = "r"
_STATE_PREFIX_NORMAL = "n"


def _make_state(allow_rebind: bool) -> str:
    """Neuen State-Token mit Rebind-Recht-Praefix erzeugen."""
    prefix = _STATE_PREFIX_REBIND if allow_rebind else _STATE_PREFIX_NORMAL
    return prefix + secrets.token_urlsafe(32)


def _state_allows_rebind(state: str) -> bool:
    """Liest aus dem State-Praefix, ob Konto-Umbiegen erlaubt ist.

    Fail-closed: unbekanntes/fehlendes Praefix -> kein Rebind.
    """
    return bool(state) and state[0] == _STATE_PREFIX_REBIND


async def _generate_auth_url_microsoft(
    tenant_slug: str, employee_slug: str | None = None,
    allow_rebind: bool = False, scope_profil: str | None = None,
) -> str:
    """Microsoft-OAuth-Autorisierungs-URL mit PKCE."""
    from urllib.parse import urlencode

    cfg = await _load_microsoft_config()
    state = _make_state(allow_rebind)
    code_verifier, code_challenge = _generate_pkce_pair()

    redirect_uri = cfg.get("redirect_uri") or _get_redirect_uri()

    params = {
        "client_id": cfg["client_id"],
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": " ".join(microsoft_scopes(scope_profil)),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }

    auth_url = f"{MICROSOFT_AUTHORIZE_URL}?{urlencode(params)}"

    async with AsyncSessionLocal() as session:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=STATE_LIFETIME_MINUTES)
        await session.execute(delete(OAuthState).where(OAuthState.created_at < cutoff))

        oauth_state = OAuthState(
            state=state,
            tenant_slug=tenant_slug,
            provider="microsoft",
            code_verifier=code_verifier,
            employee_slug=employee_slug,
            scope_profil=scope_profil,
        )
        session.add(oauth_state)
        await session.commit()

    logger.info(
        f"Microsoft-OAuth-URL generiert fuer Tenant {tenant_slug}"
        f"{('/'+employee_slug) if employee_slug else ''} (state={state[:8]}...)"
    )
    return auth_url


async def generate_auth_url(
    tenant_slug: str, provider: str = "google",
    employee_slug: str | None = None,
    allow_rebind: bool = False,
    scope_profil: str | None = None,
) -> str:
    """Erzeugt OAuth-Autorisierungs-URL und persistiert State in DB.

    Phase 1 Multi-OAuth: optional employee_slug — wird im OAuth-State
    gespeichert damit der Callback weiss FUER WEN der Token abgelegt
    werden soll.

    allow_rebind: Nur True aus dem authentifizierten PWA-Pfad. Erlaubt dem
    Callback, eine bereits verbundene Anbindung auf ein ANDERES Konto
    umzubiegen. Der oeffentliche GET-Einstieg laesst es auf False, damit
    niemand ueber einen geratenen Tenant-Slug eine fremde Kalender-/Mail-
    Anbindung auf sein eigenes Konto uebernehmen kann.
    """
    if provider == "microsoft":
        return await _generate_auth_url_microsoft(
            tenant_slug, employee_slug, allow_rebind=allow_rebind,
            scope_profil=scope_profil,
        )
    if provider != "google":
        raise NotImplementedError(f"Provider {provider} noch nicht unterstuetzt")

    client_config = _load_client_config()
    flow = Flow.from_client_config(
        client_config,
        scopes=google_scopes(scope_profil),
        redirect_uri=_get_redirect_uri(),
    )

    # PKCE (RFC 7636) explizit fuer Google aktivieren — schuetzt vor
    # Code-Interception-Attacks falls der state-Cookie irgendwie leakt.
    # Microsoft macht's selber, Google's Library generiert das Challenge
    # nur wenn flow.code_verifier vor authorization_url gesetzt ist.
    code_verifier, _ = _generate_pkce_pair()
    flow.code_verifier = code_verifier

    state = _make_state(allow_rebind)
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        # Beim engen Mitarbeiter-Profil KEINE frueheren Grants mitziehen —
        # sonst haengt Google die breiteren Scopes eines vorherigen
        # Consents desselben Kontos wieder dran und der Token waere doch
        # wieder weit.
        include_granted_scopes=(
            "false" if scope_profil == SCOPE_PROFIL_MITARBEITER else "true"
        ),
        prompt="consent",
        state=state,
        code_challenge_method="S256",
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
            employee_slug=employee_slug,
            scope_profil=scope_profil,
        )
        session.add(oauth_state)
        await session.commit()

    logger.info(
        f"OAuth-URL generiert fuer Tenant {tenant_slug}"
        f"{('/'+employee_slug) if employee_slug else ''} (state={state[:8]}...)"
    )
    return auth_url


async def _resolve_employee_id(
    session, tenant_id, employee_slug: str | None,
):
    """Phase 1 Multi-OAuth: ermittelt employee_id fuer Token-Upsert.

    - employee_slug gesetzt: Employee per (tenant_id, slug) suchen.
      Existiert keiner mit dem Slug → ValueError (Onboarding-Bug).
    - employee_slug None: Default-Employee des Tenants (immer existent
      durch Phase-0-Backfill).
    """
    from core.models.employee import Employee, get_default_employee

    if employee_slug:
        result = await session.execute(
            select(Employee).where(
                Employee.tenant_id == tenant_id,
                Employee.slug == employee_slug,
            )
        )
        emp = result.scalar_one_or_none()
        if not emp:
            raise ValueError(
                f"Employee slug='{employee_slug}' nicht gefunden "
                f"fuer tenant_id={tenant_id}"
            )
        return emp.id

    emp = await get_default_employee(tenant_id)
    return emp.id if emp else None


class OAuthRebindRejected(Exception):
    """Callback wollte eine bereits verbundene Anbindung auf ein anderes
    Konto umbiegen, ohne dazu berechtigt zu sein (Hijack-Schutz)."""


def _reject_hijack(
    oauth_token: OAuthToken,
    new_email: str,
    *,
    allow_rebind: bool,
    tenant_slug: str,
    provider: str,
) -> None:
    """Verhindert, dass ein oeffentlich gestarteter OAuth-Flow eine bereits
    verbundene Anbindung auf ein anderes Konto umbiegt.

    Erlaubt bleibt: Erst-Verbindung (noch kein refresh_token) und Reauth
    desselben Kontos (gleiche account_email). Abgelehnt wird nur der Wechsel
    auf ein ANDERES Konto ueber einen nicht dazu berechtigten (oeffentlichen)
    Flow. Der authentifizierte PWA-Pfad setzt allow_rebind=True und darf das.
    """
    if allow_rebind:
        return
    existing_email = (oauth_token.account_email or "").strip().casefold()
    already_connected = bool((oauth_token.refresh_token or "").strip())
    if not already_connected or not existing_email:
        return  # Erst-Verbindung — unkritisch
    if existing_email == (new_email or "").strip().casefold():
        return  # Reauth desselben Kontos — ok
    logger.warning(
        "OAuth-Hijack abgewehrt: tenant=%s provider=%s verbunden=%s "
        "Callback-Konto=%s (oeffentlicher Flow ohne Rebind-Recht)",
        tenant_slug, provider, oauth_token.account_email, new_email,
    )
    raise OAuthRebindRejected(
        "Diese Anbindung ist bereits mit einem anderen Konto verbunden. "
        "Ein Wechsel ist nur in der App unter Einstellungen moeglich."
    )


async def _upsert_oauth_token(
    session,
    *,
    tenant_id,
    employee_id,
    provider: str,
) -> OAuthToken:
    """Sucht bestehenden Token via (employee_id, provider) — sonst neu.

    Phase 1: bevorzugt Lookup ueber employee_id. Fallback: tenant-weiter
    Legacy-Token (employee_id IS NULL) der noch nicht migriert ist —
    den uebernehmen wir und schreiben employee_id rein.
    """
    if employee_id is not None:
        result = await session.execute(
            select(OAuthToken).where(
                OAuthToken.employee_id == employee_id,
                OAuthToken.provider == provider,
            )
        )
        oauth_token = result.scalar_one_or_none()
        if oauth_token is not None:
            return oauth_token

    # Legacy-Fallback: tenant-weiter Token ohne employee_id
    result = await session.execute(
        select(OAuthToken).where(
            OAuthToken.tenant_id == tenant_id,
            OAuthToken.provider == provider,
            OAuthToken.employee_id.is_(None),
        )
    )
    legacy = result.scalar_one_or_none()
    if legacy is not None:
        legacy.employee_id = employee_id
        return legacy

    new_token = OAuthToken(
        tenant_id=tenant_id,
        employee_id=employee_id,
        provider=provider,
    )
    session.add(new_token)
    return new_token


async def _handle_callback_microsoft(
    code: str,
    state: str,
    tenant_slug: str,
    code_verifier: str,
    employee_slug: str | None = None,
    scope_profil: str | None = None,
) -> OAuthToken:
    """Verarbeitet Microsoft-OAuth-Callback."""
    cfg = await _load_microsoft_config()
    redirect_uri = cfg.get("redirect_uri") or _get_redirect_uri()

    # Tokens via POST holen
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            MICROSOFT_TOKEN_URL,
            data={
                "client_id": cfg["client_id"],
                "client_secret": cfg["client_secret"],
                "code": code,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": code_verifier,
                "scope": " ".join(microsoft_scopes(scope_profil)),
            },
            headers={"Accept": "application/json"},
        )
        if resp.status_code != 200:
            raise ValueError(
                f"Microsoft Token-Exchange fehlgeschlagen: "
                f"{resp.status_code} {resp.text[:300]}"
            )
        tokens = resp.json()

    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    expires_in = tokens.get("expires_in", 3600)
    granted_scopes = tokens.get("scope", " ".join(MICROSOFT_SCOPES))

    if not access_token or not refresh_token:
        raise ValueError("Microsoft hat keinen access_token oder refresh_token zurueckgegeben")

    # User-Email holen via Graph API
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            MICROSOFT_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if resp.status_code != 200:
            raise ValueError(f"Microsoft Graph /me fehlgeschlagen: {resp.status_code}")
        user_info = resp.json()
        account_email = user_info.get("mail") or user_info.get("userPrincipalName") or ""

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in) - 60)

    # In DB speichern (upsert via employee_id-aware Helper)
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Tenant).where(Tenant.slug == tenant_slug)
        )
        tenant = result.scalar_one_or_none()
        if not tenant:
            raise ValueError(f"Tenant {tenant_slug} nicht gefunden")

        employee_id = await _resolve_employee_id(session, tenant.id, employee_slug)
        oauth_token = await _upsert_oauth_token(
            session,
            tenant_id=tenant.id,
            employee_id=employee_id,
            provider="microsoft",
        )
        _reject_hijack(
            oauth_token, account_email,
            allow_rebind=_state_allows_rebind(state),
            tenant_slug=tenant_slug, provider="microsoft",
        )

        oauth_token.refresh_token = refresh_token
        oauth_token.access_token = access_token
        oauth_token.scopes = granted_scopes
        oauth_token.account_email = account_email
        oauth_token.access_token_expires_at = expires_at

        await session.commit()
        await session.refresh(oauth_token)

    logger.info(
        f"Microsoft-OAuth-Token gespeichert: tenant={tenant_slug}"
        f"{('/'+employee_slug) if employee_slug else ''} email={account_email}"
    )

    # Onboarding-Check (Option 2): Mailbox-Timezone abfragen und warnen
    # wenn nicht Berlin-kompatibel. Outlook-Web/-Desktop zeigt Termine in
    # der Mailbox-Default-TZ — bei UTC-Mailboxen werden 11:00-Termine als
    # 09:00 angezeigt (siehe Incident vom 2026-05-17). Best-effort: ein
    # Fehler hier blockiert NICHT die Token-Speicherung.
    try:
        from core.integrations.microsoft_calendar import (
            get_mailbox_timezone, is_berlin_compatible_timezone,
        )
        mailbox_tz = await get_mailbox_timezone(tenant.id, employee_id=employee_id)
        if mailbox_tz is not None and not is_berlin_compatible_timezone(mailbox_tz):
            logger.warning(
                f"Outlook-Onboarding {account_email}: mailbox-TZ "
                f"{mailbox_tz!r} ist nicht Berlin-kompatibel — Termine "
                f"werden im Outlook-Client verschoben angezeigt."
            )
            try:
                from core.integrations.notify import notify_employee
                warn = (
                    "⚠️ <b>Outlook-Kalender verbunden — aber Zeitzone pruefen!</b>\n\n"
                    f"Account: <code>{account_email}</code>\n"
                    f"Deine Outlook-Mailbox steht auf <b>{mailbox_tz}</b>. "
                    f"Termine die wir um 11:00 buchen werden im Outlook-Client "
                    f"deshalb verschoben angezeigt (meist 2h frueher).\n\n"
                    f"<b>So aenderst du das (einmalig, 30 Sek):</b>\n"
                    f"Outlook-Web -> Settings (Zahnrad oben rechts) -> "
                    f"<b>Calendar -> View</b> -> Time zone: "
                    f"<code>(UTC+01:00) Amsterdam, Berlin, Bern, Rom, Stockholm, Wien</code>\n\n"
                    f"Danach passt die Anzeige. Unsere Buchungen sind "
                    f"unabhaengig davon im Hintergrund korrekt — nur die "
                    f"Outlook-Anzeige ist betroffen."
                )
                await notify_employee(
                    tenant.id, employee_id,
                    title="Outlook verbunden — Zeitzone prüfen",
                    body=(
                        f"Mailbox steht auf {mailbox_tz}. "
                        f"Anleitung in der App."
                    ),
                    url="/app#mehr", tag="tz-warnung",
                    telegram_text=warn,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"TZ-Warning-Push failed: {exc}")
        elif mailbox_tz is not None:
            logger.info(
                f"Outlook-Onboarding {account_email}: mailbox-TZ "
                f"{mailbox_tz!r} ist Berlin-kompatibel — keine Warnung."
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Mailbox-TZ-Check fehlgeschlagen (kein Blocker): {exc}")

    return oauth_token


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
        employee_slug = oauth_state.employee_slug
        # MUSS aus dem State kommen: der Token-Tausch baut den Flow mit
        # derselben Scope-Liste neu auf, sonst "Scope has changed".
        scope_profil = getattr(oauth_state, "scope_profil", None)
        await session.delete(oauth_state)
        await session.commit()

    # Provider-Routing: Microsoft-spezifischer Pfad
    if provider == "microsoft":
        return await _handle_callback_microsoft(
            code, state, tenant_slug, code_verifier, employee_slug,
            scope_profil=scope_profil,
        )

    # Token von Google holen
    client_config = _load_client_config()
    flow = Flow.from_client_config(
        client_config,
        scopes=google_scopes(scope_profil),
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

    # In DB speichern (upsert via employee_id-aware Helper)
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Tenant).where(Tenant.slug == tenant_slug)
        )
        tenant = result.scalar_one_or_none()
        if not tenant:
            raise ValueError(f"Tenant {tenant_slug} nicht gefunden")

        employee_id = await _resolve_employee_id(session, tenant.id, employee_slug)
        oauth_token = await _upsert_oauth_token(
            session,
            tenant_id=tenant.id,
            employee_id=employee_id,
            provider=provider,
        )
        _reject_hijack(
            oauth_token, account_email,
            allow_rebind=_state_allows_rebind(state),
            tenant_slug=tenant_slug, provider=provider,
        )

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
            f"OAuth-Token gespeichert: tenant={tenant_slug}"
            f"{('/'+employee_slug) if employee_slug else ''} account={account_email}"
        )
        return oauth_token
