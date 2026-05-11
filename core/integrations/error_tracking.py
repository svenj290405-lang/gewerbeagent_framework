"""Sentry-Integration (Phase B2) — optional, opt-in via SENTRY_DSN.

Sentry sammelt unbehandelte Exceptions + ASGI-Request-Context. Wir
nutzen es opt-in: wenn `SENTRY_DSN` in der Env nicht gesetzt ist (oder
das Package nicht installiert), ist alles silent skipped — kein
Boot-Fail, kein Funktionsverlust.

Aktivierung in 3 Schritten:
  1. `pyproject.toml`: `sentry-sdk[fastapi]>=2` in dependencies aufnehmen
  2. Docker-Image rebuilden (Bind-Mount reicht hier NICHT, weil neue
     Python-Deps installiert werden muessen)
  3. `.env`: SENTRY_DSN=<dsn aus Sentry-Project> setzen
  4. Optional: SENTRY_ENVIRONMENT=production, SENTRY_TRACES_SAMPLE_RATE=0.1

Self-Hosted-Alternative: GlitchTip (api-kompatibel, gleicher SDK).
Setze einfach den GlitchTip-DSN als SENTRY_DSN — kein Code-Aenderung
noetig.

Release-Tagging: wir taggen jeden Sentry-Event mit dem aktuellen
git commit hash (env GIT_COMMIT_SHA, gesetzt vom deploy_prod.sh).
Damit korrelieren Bugs sauber mit Deploys.

Sven-Push fuer kritische Fehler: separate notify_sven_admin_alert-
Pfad in admin_alerts.py. Sentry liefert die Aggregation + Stack-Trace
zur Forensik.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def init_sentry() -> bool:
    """Initialisiert Sentry-SDK, wenn DSN + Package vorhanden.

    Returns:
        True wenn Sentry aktiv, False wenn (silent) skipped.
    """
    dsn = (os.environ.get("SENTRY_DSN") or "").strip()
    if not dsn:
        logger.debug("Sentry: SENTRY_DSN nicht gesetzt — error-tracking aus")
        return False

    try:
        import sentry_sdk  # type: ignore[import-untyped]
        from sentry_sdk.integrations.fastapi import (  # type: ignore[import-untyped]
            FastApiIntegration,
        )
        from sentry_sdk.integrations.sqlalchemy import (  # type: ignore[import-untyped]
            SqlalchemyIntegration,
        )
    except ImportError:
        logger.warning(
            "Sentry: SENTRY_DSN gesetzt aber sentry-sdk nicht installiert. "
            "Skipping. `uv add sentry-sdk[fastapi]` + Image-Rebuild zum Aktivieren."
        )
        return False

    release = os.environ.get("GIT_COMMIT_SHA") or "unknown"
    environment = os.environ.get("SENTRY_ENVIRONMENT") or "production"
    sample_rate = float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0") or "0")

    sentry_sdk.init(
        dsn=dsn,
        release=f"gewerbeagent@{release}",
        environment=environment,
        traces_sample_rate=sample_rate,   # 0 = nur Errors, kein APM
        # Failsafe: keine Auto-Performance-Spans damit der App-Footprint
        # niedrig bleibt. Tenant-PII (Mail-Adressen etc.) wird automatisch
        # gescrubbed via send_default_pii=False (Sentry-Default).
        send_default_pii=False,
        integrations=[
            FastApiIntegration(transaction_style="endpoint"),
            SqlalchemyIntegration(),
        ],
        before_send=_scrub_sensitive_event,
    )
    logger.info(
        f"Sentry initialisiert: env={environment} release={release[:12]}"
    )
    return True


def _scrub_sensitive_event(event: dict, hint: dict) -> dict | None:
    """before_send-Hook: scrubbt sensible Felder aus dem Event.

    - Headers: Authorization, Cookie, X-Telegram-Bot-Api-Secret-Token
    - QueryParams: code, state, token, password
    - Form: api_key, password, token

    Sentry hat schon eingebauten PII-Stripping, das ist defense-in-depth.
    """
    request = event.get("request") or {}
    headers = request.get("headers") or {}
    for sensitive in (
        "authorization", "cookie", "x-telegram-bot-api-secret-token",
        "elevenlabs-signature", "x-webhook-secret",
    ):
        for key in list(headers.keys()):
            if key.lower() == sensitive:
                headers[key] = "[REDACTED]"

    query = request.get("query_string") or ""
    if isinstance(query, str) and ("code=" in query or "token=" in query):
        request["query_string"] = "[REDACTED]"

    return event


async def capture_admin_alert_exception(exc: BaseException, *, hint: str) -> None:
    """Manuell-Push einer Exception nach Sentry — fuer interessante
    Failure-Pfade die wir nicht auto-aggregieren wollen.

    No-op wenn Sentry nicht aktiv.
    """
    try:
        import sentry_sdk  # type: ignore[import-untyped]
        sentry_sdk.capture_exception(exc)
        sentry_sdk.capture_message(hint, level="error")
    except ImportError:
        pass
    except Exception as e:
        logger.debug(f"capture_admin_alert_exception failed (egal): {e}")
