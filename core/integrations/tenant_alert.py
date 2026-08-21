"""
Tenant-Alert-Pipeline.

Zentrale Stelle fuer alle proaktiven Benachrichtigungen an den Tenant
per Web-Push. Verhindert dass kritische Probleme (Token expired,
Cron tot, API-Limit erreicht) lautlos im Container-Log versanden.

Design:
- Failsafe: Alert-Fehler bricht NIE den Caller ab.
- Throttle: gleiche Alert-Art pro Tenant max 1x / 6h damit der Tenant
  nicht mit Pushes ueberflutet wird wenn ein Cron 1000x failsiert.
- Mit Action-Hint: jeder Alert sagt was der Tenant tun soll.
"""
from __future__ import annotations

import datetime as dt
import logging
from uuid import UUID

from sqlalchemy import desc, select

from core.database.connection import get_session
from core.models.admin import AdminAuditLog

logger = logging.getLogger(__name__)


# Mindestabstand zwischen identischen Alerts pro Tenant.
ALERT_COOLDOWN_HOURS = 6


async def _was_recently_alerted(
    *, tenant_id: UUID, alert_kind: str,
    cooldown_hours: int = ALERT_COOLDOWN_HOURS,
) -> bool:
    """True wenn dieser Alert in cooldown_hours schon mal raus ging.

    Wir nutzen admin_audit_log.action wie 'alert.<kind>' als Marker —
    damit kein neues DB-Schema noetig ist.
    """
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=cooldown_hours)
    action = f"alert.{alert_kind}"
    target = str(tenant_id)
    async with get_session() as s:
        stmt = (
            select(AdminAuditLog.id)
            .where(AdminAuditLog.action == action)
            .where(AdminAuditLog.target == target)
            .where(AdminAuditLog.created_at >= since)
            .order_by(desc(AdminAuditLog.created_at))
            .limit(1)
        )
        return (await s.execute(stmt)).scalar_one_or_none() is not None


async def _record_alert(
    *, tenant_id: UUID, alert_kind: str, success: bool,
    details: dict | None = None,
) -> None:
    """Markiert dass ein Alert versucht wurde (auch bei Push-Fehler)."""
    try:
        async with get_session() as s:
            row = AdminAuditLog(
                user_id=None,
                action=f"alert.{alert_kind}"[:80],
                target=str(tenant_id)[:255],
                ip_address=None,
                user_agent="tenant-alert-pipeline",
                success=success,
                details=details,
            )
            s.add(row)
    except Exception as e:
        logger.debug(f"alert audit log failed (egal): {e}")


async def _send_alert(
    *, tenant_id: UUID, message: str,
    employee_id: UUID | None = None,
) -> bool:
    """Benachrichtigt den Betrieb per Web-Push.

    Failsafe: ein Alert-Fehler darf den Aufrufer nie abbrechen. Die
    ``message`` bleibt bewusst im Server (sie kann Kunden-PII enthalten) —
    der Push sagt nur, dass etwas anliegt, die Details holt die App.
    """
    try:
        from core.integrations.push_notifier import send_push_to_tenant
        return bool(await send_push_to_tenant(
            tenant_id,
            title="Hinweis zu deinem Betrieb",
            body="In der App ansehen.",
            url="/app#aktuelles", tag="tenant-alert",
        ))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"_send_alert push failed: {e}")
        return False


async def notify_oauth_revoked(
    *, tenant_id: UUID, provider: str,
    employee_id: UUID | None = None,
) -> None:
    """OAuth-Token vom User entzogen oder abgelaufen ohne Refresh.

    Wrapper auf core.security.oauth_alert.notify_oauth_token_invalid —
    konsolidiert auf eine einzige Implementierung mit Re-Auth-Link,
    Schritt-fuer-Schritt zur Google-Verifizierungs-Warnung und
    Kontakt-Hinweis. employee_id wird ignoriert (Push geht an Tenant-
    Default — der Inhaber muss neu autorisieren, nicht Mitarbeiter).
    """
    from core.security.oauth_alert import notify_oauth_token_invalid

    await notify_oauth_token_invalid(
        tenant_id, provider, reason="refresh_failed",
    )


async def notify_lexware_dead(*, tenant_id: UUID, days_silent: int) -> None:
    """Lexware-Polling ist seit X Tagen ohne Erfolg — vermutlich Key tot."""
    alert_kind = "lexware_dead"
    if await _was_recently_alerted(
        tenant_id=tenant_id, alert_kind=alert_kind, cooldown_hours=24,
    ):
        return
    msg = (
        f"⚠️ <b>Lexware-Verbindung scheint tot</b>\n\n"
        f"Seit {days_silent} Tagen kein erfolgreicher API-Call. "
        f"Bitte in den Einstellungen den Lexware-API-Key pruefen.\n\n"
        f"Bis dahin wird der Bezahl-Status der Rechnungen nicht aktualisiert."
    )
    sent = await _send_alert(tenant_id=tenant_id, message=msg)
    await _record_alert(
        tenant_id=tenant_id, alert_kind=alert_kind, success=sent,
        details={"days_silent": days_silent},
    )


async def notify_cron_dead(*, cron_name: str, last_heartbeat_minutes: int) -> None:
    """Globaler Cron hat seit > N Minuten keinen Heartbeat geschrieben.

    Wird an den ersten Admin geschickt (nicht per Tenant).
    """
    alert_kind = f"cron_dead.{cron_name}"
    # Globaler Alert — Tenant_id wird der erste Admin-User
    try:
        from core.models.admin import AdminUser
        async with get_session() as s:
            admin = (await s.execute(
                select(AdminUser).where(AdminUser.is_active.is_(True)).limit(1)
            )).scalar_one_or_none()
        if not admin:
            logger.warning(f"notify_cron_dead({cron_name}): kein Admin gefunden")
            return
        # Audit-Log mit user_id=admin.id, action='alert.cron_dead.<name>'
        action = f"alert.{alert_kind}"
        async with get_session() as s:
            recent = (await s.execute(
                select(AdminAuditLog.id)
                .where(AdminAuditLog.action == action)
                .where(AdminAuditLog.created_at >=
                       dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1))
                .limit(1)
            )).scalar_one_or_none()
        if recent:
            return  # cooldown
        async with get_session() as s:
            s.add(AdminAuditLog(
                user_id=admin.id, action=action[:80],
                target=cron_name[:255], success=False,
                details={"last_heartbeat_minutes": last_heartbeat_minutes},
            ))
        logger.error(
            f"CRON DEAD: {cron_name} kein Heartbeat seit "
            f"{last_heartbeat_minutes} min — Audit-Log geschrieben"
        )
    except Exception as e:
        logger.warning(f"notify_cron_dead failed: {e}")
