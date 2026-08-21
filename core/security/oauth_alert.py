"""OAuth-Token-Invalid-Alarm: Push an den Tenant wenn der
Refresh-Token eines Providers (Google/Microsoft) abgelaufen oder
revoked ist.

Hintergrund: bisher loggten wir invalid_grant nur als WARNING — der
Tenant merkte erst Stunden/Tage spaeter dass Drive-Archiv leise
gestorben ist. Jetzt bekommt er einen Push; die Re-Auth-Schritte
stehen in der App unter "Mehr".

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


async def notify_oauth_token_invalid(
    tenant_id: UUID,
    provider: str,
    *,
    reason: str | None = None,
) -> bool:
    """Schickt einen Push an den Tenant, dass der OAuth-Token
    fuer `provider` (=google|microsoft) re-authorized werden muss.

    Throttled: max. 1 Push pro 6h pro (tenant_id, provider). Wiederholte
    Aufrufe im Throttle-Fenster sind no-op (False return).

    Returns: True wenn ein Push abgeschickt wurde, False wenn
    geskippt (Throttle oder Tenant nicht gefunden).
    """
    from sqlalchemy import select
    from core.database import AsyncSessionLocal
    from core.models import Tenant
    from core.integrations.notify import notify_tenant

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

    try:
        ok = await notify_tenant(
            tenant_id,
            title="Verbindung unterbrochen",
            body=(
                f"Die {provider.capitalize()}-Verbindung muss neu "
                f"hergestellt werden. In der App öffnen."
            ),
            url="/app#mehr", tag=f"oauth-{provider}",
            inhaber_only=True,
        )
    except Exception as e:
        logger.warning(
            f"notify_oauth_token_invalid: Versand fehlgeschlagen "
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
