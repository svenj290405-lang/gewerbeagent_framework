"""OAuth-Token-Invalid-Alarm: Telegram-Push an Tenant wenn der
Refresh-Token eines Providers (Google/Microsoft) abgelaufen oder
revoked ist.

Hintergrund: bisher loggten wir invalid_grant nur als WARNING — der
Tenant merkte erst Stunden/Tage spaeter dass Drive-Archiv leise
gestorben ist. Mit diesem Helper kriegt er einen Push mit klickbarem
Re-Auth-Link, einer Mini-Anleitung zur Google-"unverified app"-Warnung
und einem Kontakt-Hinweis fuer Rueckfragen.

Throttling: max. 1 Push pro 6h pro (tenant_id, provider) — sonst spammt
es bei retry-loops in google_drive/kalender (jede fehlgeschlagene
Drive-Operation ruft den Helper auf).

Throttle-State ist in-memory (Prozess-lokal). Nach Container-Restart
gibt's einen frischen Push, was OK ist: der Restart selbst ist selten
und der Tenant soll wieder informiert werden falls er den vorigen
Push verpasst hat.
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Final
from uuid import UUID

logger = logging.getLogger(__name__)


# Throttle-Fenster: 6h zwischen zwei Alarmen pro (tenant, provider)
_ALERT_THROTTLE_SECONDS: Final[int] = 6 * 60 * 60

# In-memory State: {(tenant_id, provider): last_alert_at_utc}
_LAST_ALERT_AT: dict[tuple[UUID, str], _dt.datetime] = {}


PROVIDER_LABEL = {
    "google": "Google-Konto (Drive + Kalender)",
    "microsoft": "Microsoft-Konto (Outlook + Kalender)",
}


def _build_push_text(
    provider: str,
    reauth_url: str,
    tenant_name: str | None,
) -> str:
    """Baut den HTML-Telegram-Push.

    Inhalt:
    - Was kaputt ist
    - Re-Auth-Link
    - Schritt-fuer-Schritt fuer die Google-"unverified app"-Warnung
      (entfaellt fuer Microsoft, kein Warn-Screen dort)
    - Hinweis bei Rueckfragen an Sven
    """
    from html import escape as _h

    label = PROVIDER_LABEL.get(provider, provider)
    tenant_str = f" ({_h(tenant_name)})" if tenant_name else ""

    text = (
        f"⚠️ <b>{_h(label)} muss neu verbunden werden</b>{tenant_str}\n\n"
        f"Q kann gerade nicht mehr auf das Konto zugreifen — die "
        f"OAuth-Verbindung ist abgelaufen oder wurde widerrufen.\n\n"
        f"👉 <b>Hier neu verbinden:</b>\n"
        f"<a href=\"{_h(reauth_url)}\">{_h(reauth_url)}</a>\n\n"
    )

    if provider == "google":
        text += (
            "Beim Klick passiert das:\n"
            "1. Google-Login mit dem Geschaefts-Account\n"
            "2. Bildschirm <b>\"Google hat diese App nicht verifiziert\"</b> "
            "erscheint — bitte annehmen:\n"
            "   • unten auf <b>\"Erweitert\"</b> tippen\n"
            "   • dann <b>\"peppy-winter-492820-h3 öffnen (unsicher)\"</b>\n"
            "3. Berechtigungen (Drive + Kalender) zulassen\n"
            "4. Fertig — Q laeuft sofort wieder.\n\n"
            "Der Warnscreen kommt nur weil die App noch nicht von Google "
            "verifiziert ist. Das ist sicher fuer Sie — Sie geben den "
            "Zugriff an Ihre eigene Q-Instanz.\n\n"
        )
    elif provider == "microsoft":
        text += (
            "Beim Klick passiert das:\n"
            "1. Microsoft-Login mit dem Geschaefts-Account\n"
            "2. Berechtigungen (Mail + Kalender) zulassen\n"
            "3. Fertig — Q laeuft sofort wieder.\n\n"
        )

    text += (
        "Bei Fragen oder Problemen: an <b>Sven</b> wenden."
    )
    return text


async def notify_oauth_token_invalid(
    tenant_id: UUID,
    provider: str,
    *,
    reason: str | None = None,
) -> bool:
    """Schickt einen Telegram-Push an den Tenant, dass der OAuth-Token
    fuer `provider` (=google|microsoft) re-authorized werden muss.

    Throttled: max. 1 Push pro 6h pro (tenant_id, provider). Wiederholte
    Aufrufe im Throttle-Fenster sind no-op (False return).

    Returns: True wenn ein Push abgeschickt wurde, False wenn
    geskippt (Throttle, kein Telegram, Tenant nicht gefunden).
    """
    from sqlalchemy import select
    from config.settings import settings
    from core.database import AsyncSessionLocal
    from core.models import Tenant
    from plugins.telegram_notify.handler import TelegramNotifier

    if provider not in ("google", "microsoft"):
        logger.warning(f"notify_oauth_token_invalid: unbekannter provider={provider!r}")
        return False

    # Throttle-Check
    key = (tenant_id, provider)
    now = _dt.datetime.now(_dt.timezone.utc)
    last = _LAST_ALERT_AT.get(key)
    if last is not None:
        delta = (now - last).total_seconds()
        if delta < _ALERT_THROTTLE_SECONDS:
            logger.debug(
                f"notify_oauth_token_invalid: throttled tenant={tenant_id} "
                f"provider={provider} (letzter Alarm vor {int(delta)}s)"
            )
            return False

    # Tenant laden fuer Slug + Anzeigename
    async with AsyncSessionLocal() as session:
        r = await session.execute(
            select(Tenant).where(Tenant.id == tenant_id)
        )
        tenant = r.scalar_one_or_none()
    if tenant is None:
        logger.warning(
            f"notify_oauth_token_invalid: tenant_id={tenant_id} nicht gefunden"
        )
        return False

    base = (settings.public_url or "").rstrip("/")
    reauth_url = f"{base}/oauth/start?tenant={tenant.slug}&provider={provider}"
    text = _build_push_text(
        provider=provider,
        reauth_url=reauth_url,
        tenant_name=tenant.company_name,
    )

    try:
        ok = await TelegramNotifier.send_for_tenant(tenant_id, text)
    except Exception as e:
        logger.warning(
            f"notify_oauth_token_invalid: Telegram-Send fehlgeschlagen "
            f"tenant={tenant.slug} provider={provider}: {e}"
        )
        return False

    if ok:
        _LAST_ALERT_AT[key] = now
        logger.info(
            f"notify_oauth_token_invalid: Push gesendet tenant={tenant.slug} "
            f"provider={provider} reason={(reason or '')[:80]!r}"
        )
        return True
    return False


def _reset_throttle_for_tests() -> None:
    """Nur fuer Tests — leert den Throttle-Cache."""
    _LAST_ALERT_AT.clear()


def is_oauth_invalid_error(exc: BaseException) -> bool:
    """Heuristik: handelt es sich um einen 'Refresh-Token ist abgelaufen
    oder revoked'-Fehler?

    Pattern in den Stack-Traces der Provider:
    - Google: `google.auth.exceptions.RefreshError` mit body
      `{'error': 'invalid_grant', ...}`. Wir matchen 'invalid_grant'
      im str(exc).
    - Microsoft: HTTP 400 mit body 'AADSTS70043' (User must reauth) /
      'AADSTS50173' / 'AADSTS54005'.

    Bewusst auf String-Match basiert weil die Exception-Klassen je
    nach SDK-Version variieren — kein hartes Klassen-Coupling.
    """
    s = (str(exc) or "").lower()
    google_marker = "invalid_grant" in s or "token has been expired" in s
    microsoft_marker = any(
        code in s for code in ("aadsts70043", "aadsts50173", "aadsts54005")
    ) or "must reauthenticate" in s
    return google_marker or microsoft_marker
