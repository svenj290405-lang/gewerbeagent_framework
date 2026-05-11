"""Admin-Alert-Pipeline (Sven-Benachrichtigungen).

Schwester-Modul zu `tenant_alert.py` — gleicher Grundgedanke, nur:
- Empfaenger: Sven (settings.admin_telegram_*), nicht Tenant-User
- Throttle: 1h Cooldown pro alert_kind (Tenant: 6h)
- Failsafe: jeder Fehler wird verschluckt, ein Alert darf nie den
  Caller abbrechen

Typische Caller:
- Cron-Health-Watchdog (external liveness, DB down, Cron dead)
- Silent-Pipeline-Failures (Drive-Upload-Series-Fail, Mail-Klassifikation
  hängt 24h)
- Mail-Retry-Queue dead-letter

API-Vertrag:
    await notify_sven_admin_alert(
        kind="framework_down",
        message="⚠️ Framework antwortet seit 15 min nicht",
        details={"last_status": 503, "tries": 3},
    )
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import httpx
from sqlalchemy import desc, select

from config.settings import settings
from core.database.connection import get_session
from core.models.admin import AdminAuditLog, AdminUser

logger = logging.getLogger(__name__)


# Mindestabstand zwischen identischen Sven-Alerts. 1h ist eine Balance
# zwischen "Sven nicht zuspammen" und "Sven muss Bescheid wissen wenn
# was wirklich kaputt ist".
ADMIN_ALERT_COOLDOWN_HOURS = 1


async def _was_admin_recently_alerted(*, kind: str, cooldown_hours: int) -> bool:
    """True wenn dieser Alert in cooldown_hours schon mal raus ging."""
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=cooldown_hours)
    action = f"sven_alert.{kind}"
    try:
        async with get_session() as s:
            stmt = (
                select(AdminAuditLog.id)
                .where(AdminAuditLog.action == action)
                .where(AdminAuditLog.created_at >= since)
                .order_by(desc(AdminAuditLog.created_at))
                .limit(1)
            )
            return (await s.execute(stmt)).scalar_one_or_none() is not None
    except Exception as e:
        # Wenn DB tot ist (was ja moeglich ist wenn dieser Alert ueberhaupt
        # geschickt wird), trotzdem versuchen zu pushen. Cooldown wird
        # dann ignoriert — besser doppelt warnen als gar nicht.
        logger.debug(f"_was_admin_recently_alerted DB-error (ignore): {e}")
        return False


async def _record_admin_alert(
    *, kind: str, success: bool, details: dict[str, Any] | None = None,
) -> None:
    """Markiert dass ein Sven-Alert versucht wurde (Erfolg + Details)."""
    try:
        async with get_session() as s:
            # user_id muss auf einen existierenden Admin zeigen (FK).
            # Wir nehmen den ersten aktiven Admin — bei DB-Problemen
            # gerade silent skip.
            admin = (await s.execute(
                select(AdminUser).where(AdminUser.is_active.is_(True)).limit(1)
            )).scalar_one_or_none()
            row = AdminAuditLog(
                user_id=admin.id if admin else None,
                action=f"sven_alert.{kind}"[:80],
                target=kind[:255],
                ip_address=None,
                user_agent="admin-alert-pipeline",
                success=success,
                details=details,
            )
            s.add(row)
    except Exception as e:
        logger.debug(f"admin alert audit log failed (egal): {e}")


async def _send_telegram_to_sven(message: str) -> bool:
    """Direkter Telegram-API-Call an Sven's Admin-Chat. Failsafe."""
    token = settings.admin_telegram_bot_token
    chat_id = settings.admin_telegram_chat_id
    if not token or not chat_id:
        logger.warning(
            "Sven-Alert nicht zustellbar — admin_telegram_bot_token oder "
            "admin_telegram_chat_id ist nicht gesetzt"
        )
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": message[:4000],  # Telegram-Limit 4096
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
            ok = resp.status_code == 200
            if not ok:
                logger.warning(
                    "Sven-Alert HTTP %d: %s", resp.status_code, resp.text[:200]
                )
            return ok
    except Exception as e:
        logger.warning(f"_send_telegram_to_sven failed: {e}")
        return False


async def notify_sven_admin_alert(
    *,
    kind: str,
    message: str,
    details: dict[str, Any] | None = None,
    cooldown_hours: int = ADMIN_ALERT_COOLDOWN_HOURS,
    bypass_cooldown: bool = False,
) -> bool:
    """Schickt eine kritische Push-Notification an Sven.

    Args:
        kind: kurzer Identifier fuer Cooldown-Gruppierung, z.B.
            'framework_down', 'cron_dead.microsoft', 'drive_upload_loop'.
        message: HTML-formatierte Telegram-Nachricht (max 4000 chars).
        details: optionale Strukturdaten fuer Audit-Log.
        cooldown_hours: Mindestabstand zu identischem Alert (default 1h).
        bypass_cooldown: True = Cooldown ignorieren (fuer
            Recovery-Nachrichten z.B. "wieder online").

    Returns:
        True wenn Telegram-Push erfolgreich versendet wurde, False sonst.
        Erfolg-False heisst NICHT, dass der Caller einen Retry machen
        sollte — der Caller sollte den Alert "fire & forget" behandeln.
    """
    if not bypass_cooldown:
        if await _was_admin_recently_alerted(
            kind=kind, cooldown_hours=cooldown_hours,
        ):
            logger.info(
                f"Sven-Alert '{kind}' unterdrueckt (cooldown {cooldown_hours}h)"
            )
            return False

    sent = await _send_telegram_to_sven(message)
    await _record_admin_alert(kind=kind, success=sent, details=details)
    if sent:
        logger.info(f"Sven-Alert '{kind}' gesendet")
    return sent


async def notify_sven_recovery(*, kind: str, message: str) -> bool:
    """Convenience: Recovery-Push ohne Cooldown (z.B. 'wieder online').

    Setzt kind='recovery.<kind>' und ignoriert den Cooldown — recovery
    sollte immer durchgehen, ist aber im Audit-Log mit Prefix sichtbar.
    """
    return await notify_sven_admin_alert(
        kind=f"recovery.{kind}",
        message=message,
        bypass_cooldown=True,
    )
