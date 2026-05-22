"""Strukturiertes Logging mit Tenant-/Employee-Context.

Im Multi-Tenant-Stack sind die meisten Logs ohne Tenant-Info schwer
auswertbar — 5 verschiedene Tenants in einer Cron-Lauf-Logzeile lassen
sich nicht trennen. Wir setzen einen Python-`contextvars.ContextVar`
fuer tenant_id + employee_id pro Request/Cron-Tick, und der
Log-Formatter haengt das automatisch an jede Zeile.

Verwendung im Handler-Code:

    from core.logging_context import set_log_tenant, set_log_employee

    set_log_tenant(tenant.id)
    set_log_employee(employee.id)
    logger.info("Anfrage verarbeitet")
    # → "2026-05-11T08:00:00 INFO ... [tenant=406013f4 emp=12345678] Anfrage verarbeitet"

`set_log_tenant(None)` setzt den Context wieder leer (z.B. nach Request).
Beim Verschachteln (Sub-Tasks) muss der Caller wiederherstellen.

Im Microsoft-Cron / Mail-Retry-Cron / DSGVO-Cleanup setzen wir den
Context pro Tenant-Iteration.
"""
from __future__ import annotations

import contextvars
import logging
import re
from typing import Any
from uuid import UUID

# ContextVars sind pro-AsyncTask, pro-Thread. Perfekt fuer FastAPI +
# Asyncio-Crons.
_log_tenant_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "log_tenant_id", default=None,
)
_log_employee_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "log_employee_id", default=None,
)
# Voller (NICHT gekuerzter) Tenant-Slug des aktuellen Webhook-Requests.
# set_log_tenant kuerzt auf 8 Hex-Zeichen (Recon-Schutz) und taugt daher
# NICHT zur Tenant-Aufloesung. Diese Var traegt den vollen Slug aus dem
# Webhook-Pfad — z.B. um den richtigen Telegram-Bot-Token pro Betrieb
# aufzuloesen (eigener Bot pro Betrieb).
_webhook_tenant_slug: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "webhook_tenant_slug", default=None,
)


def set_webhook_tenant_slug(slug: str | None) -> None:
    """Setzt den vollen Tenant-Slug des aktuellen Webhook-Requests."""
    _webhook_tenant_slug.set(slug or None)


def get_webhook_tenant_slug() -> str | None:
    """Liefert den vollen Tenant-Slug des aktuellen Webhook-Requests."""
    return _webhook_tenant_slug.get()


def set_log_tenant(tenant_id: UUID | str | None) -> None:
    """Setzt den Tenant-Context fuer alle nachfolgenden Logs.

    UUID wird auf die ersten 8 Hex-Chars gekuerzt (Recon-Schutz —
    full-UUIDs im Log koennten via Log-Aggregation als Tenant-Identifier
    gegen einen Angreifer benutzt werden).
    """
    if tenant_id is None:
        _log_tenant_id.set(None)
        return
    s = str(tenant_id).replace("-", "")[:8]
    _log_tenant_id.set(s)


def set_log_employee(employee_id: UUID | str | None) -> None:
    if employee_id is None:
        _log_employee_id.set(None)
        return
    s = str(employee_id).replace("-", "")[:8]
    _log_employee_id.set(s)


def get_log_context() -> dict[str, str | None]:
    return {
        "tenant": _log_tenant_id.get(),
        "employee": _log_employee_id.get(),
    }


class TenantContextFilter(logging.Filter):
    """Logging-Filter der den ContextVar-Inhalt an jeden LogRecord haengt.

    Wird vom Formatter via `%(tenant)s` / `%(employee)s` ausgelesen.
    Wenn kein Tenant gesetzt ist (z.B. Startup-Phase), kommt "—".
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.tenant = _log_tenant_id.get() or "—"
        record.employee = _log_employee_id.get() or "—"
        return True


# =====================================================================
# SECRET-REDACTION
# =====================================================================
#
# Verhindert, dass Geheimnisse im Klartext in den Logs landen. Der
# httpx-Logger schreibt z.B. jede Anfrage-URL — inklusive
# `api.telegram.org/bot<TOKEN>/sendMessage`, womit der Bot-Token offen
# in den App-Logs (und damit 14 Tage in den Caddy-Logs) liegt. Wir
# daempfen httpx zwar auf WARNING (siehe configure_structured_logging),
# aber ein zusaetzlicher Redaction-Filter am Formatter faengt auch
# Token ab, die App-Code versehentlich selbst loggt.
_REDACTION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Telegram-Bot-Token: <bot-id>:<35-Zeichen-Secret>, oft als
    # "bot12345:AA..."-Pfadsegment in der API-URL.
    (re.compile(r"bot\d{6,}:[A-Za-z0-9_-]{20,}"), "bot<redacted>"),
    (re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"), "<redacted-token>"),
)


def _redact_secrets(text: str) -> str:
    for pattern, replacement in _REDACTION_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFormatter(logging.Formatter):
    """Formatter, der nach der ueblichen Formatierung bekannte Secrets
    aus der fertigen Logzeile maskiert (Telegram-Bot-Token etc.)."""

    def format(self, record: logging.LogRecord) -> str:
        return _redact_secrets(super().format(record))


def configure_structured_logging(*, level: str = "INFO") -> None:
    """Richtet das Root-Logger-Format mit Tenant-Context ein.

    Idempotent — bei zweitem Aufruf werden bestehende Handler ersetzt
    (z.B. Reload waehrend Entwicklung).
    """
    fmt = (
        "%(asctime)s [%(levelname)s] %(name)s "
        "[tenant=%(tenant)s emp=%(employee)s] %(message)s"
    )
    root = logging.getLogger()
    # Bestehende Handler entfernen damit der Filter nur einmal angefuegt
    # wird (sonst doppelte Logs).
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter(fmt))
    handler.addFilter(TenantContextFilter())
    root.addHandler(handler)
    root.setLevel(level)

    # httpx/httpcore loggen jede Request-URL auf INFO — inklusive des
    # Telegram-Bot-Tokens im Pfad (api.telegram.org/bot<TOKEN>/...).
    # Auf WARNING heben, damit diese URLs gar nicht erst entstehen.
    # (Der RedactingFormatter ist die zweite Verteidigungslinie.)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


# Convenience: kurzer Helper fuer Cron-Loops, die pro Tenant iterieren.
class log_tenant:
    """Context-Manager der den Tenant-Context temporaer setzt.

    Verwendung:
        async for tenant in tenants:
            with log_tenant(tenant.id):
                logger.info("processing")  # mit tenant=8hex
    """

    def __init__(self, tenant_id: UUID | str | None) -> None:
        self.tenant_id = tenant_id
        self._token: contextvars.Token[Any] | None = None

    def __enter__(self) -> "log_tenant":
        s = None
        if self.tenant_id is not None:
            s = str(self.tenant_id).replace("-", "")[:8]
        self._token = _log_tenant_id.set(s)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._token is not None:
            _log_tenant_id.reset(self._token)
