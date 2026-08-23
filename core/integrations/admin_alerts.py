"""Admin-Alert-Pipeline (Sven-Benachrichtigungen).

Schwester-Modul zu `tenant_alert.py` — gleicher Grundgedanke, nur:
- Empfaenger: der Betreiber (Sven), nicht Tenant-User
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
import os
from typing import Any

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


def _smtp_konfiguriert() -> bool:
    return bool(
        settings.alert_smtp_host
        and (settings.alert_smtp_to or settings.health_alert_email)
    )


def _sende_smtp_blockierend(betreff: str, text: str) -> None:
    """Verschickt die Alarmmail ueber ein FREMDES Postfach (Stdlib).

    Bewusst smtplib statt der eigenen Graph-Pipeline: wenn das
    Outlook-Postfach oder dessen Token der Ausfallgrund ist, kann der
    Alarm nicht ueber genau diesen Weg hinaus.
    """
    import smtplib
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["Subject"] = betreff[:200]
    msg["From"] = settings.alert_smtp_from or settings.alert_smtp_user
    msg["To"] = settings.alert_smtp_to or settings.health_alert_email
    msg.set_content(text)

    if settings.alert_smtp_starttls:
        with smtplib.SMTP(
            settings.alert_smtp_host, settings.alert_smtp_port, timeout=20,
        ) as srv:
            srv.starttls()
            if settings.alert_smtp_user:
                srv.login(settings.alert_smtp_user, settings.alert_smtp_password)
            srv.send_message(msg)
    else:
        with smtplib.SMTP_SSL(
            settings.alert_smtp_host, settings.alert_smtp_port, timeout=20,
        ) as srv:
            if settings.alert_smtp_user:
                srv.login(settings.alert_smtp_user, settings.alert_smtp_password)
            srv.send_message(msg)


async def _sende_push_an_betreiber(kind: str, message: str) -> int:
    """Web-Push an die PWA des Betreibers.

    Zweitweg, kein Ersatz fuer die Mail: er braucht den laufenden
    Container, der im Ernstfall selbst das Problem sein kann. Dafuer ist
    er sofort da und kostet nichts.
    """
    from core.integrations.push_notifier import (
        push_enabled, send_push_to_employee,
    )
    if not push_enabled():
        return 0
    from core.security.app_auth import find_employee_by_email

    ziel = settings.alert_smtp_to or settings.health_alert_email
    async with get_session() as s:
        emp = await find_employee_by_email(ziel, session=s)
        emp_id = emp.id if emp else None
    if emp_id is None:
        return 0
    return await send_push_to_employee(
        emp_id, title="Systemwarnung", body=message[:180],
        url="/app", tag=f"alert-{kind}"[:60],
    )


async def _deliver_to_sven(kind: str, message: str) -> bool:
    """Zustellung an den Betreiber ueber zwei unabhaengige Wege.

    Vorgeschichte: mit dem Telegram-Bot (entfernt am 2026-08-21,
    Drittland-Transfer) verschwand der einzige Alarmweg. Seitdem gab
    diese Funktion hart False zurueck — ein Ausfall waere niemandem
    aufgefallen, ausser jemand liest zufaellig das Container-Log.

    Reihenfolge:
      1. SMTP ueber ein FREMDES Postfach (settings.alert_smtp_*).
         Unabhaengig von Graph und vom eigenen Postfach, also auch dann
         noch da, wenn genau die Mail-Pipeline der Ausfallgrund ist.
      2. Web-Push an die PWA des Betreibers — sofort da, braucht aber
         den laufenden Container, deshalb nur Zweitweg.

    Der erste Erfolg zaehlt. Bleiben beide stumm, ist die Rueckgabe
    weiterhin False, damit sich niemand auf eine Zustellung verlaesst,
    die es nicht gab.
    """
    import asyncio

    betreff = f"[Gewerbeagent] {kind}"
    zugestellt = False

    if _smtp_konfiguriert():
        try:
            await asyncio.wait_for(
                asyncio.to_thread(_sende_smtp_blockierend, betreff, message),
                timeout=30,
            )
            zugestellt = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Alarm-SMTP fehlgeschlagen: %s", exc)

    try:
        if await _sende_push_an_betreiber(kind, message) > 0:
            zugestellt = True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Alarm-Push fehlgeschlagen: %s", exc)

    if not zugestellt:
        logger.error(
            "SVEN-ALERT [%s] NICHT zugestellt (SMTP: %s, Push: kein "
            "Empfaenger oder kein Geraet): %s",
            kind,
            "fehlgeschlagen" if _smtp_konfiguriert() else "nicht konfiguriert",
            message[:800],
        )
    else:
        logger.info("SVEN-ALERT [%s] zugestellt.", kind)
    return zugestellt


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
        message: Alarmtext (wird gekuerzt geloggt).
        details: optionale Strukturdaten fuer Audit-Log.
        cooldown_hours: Mindestabstand zu identischem Alert (default 1h).
        bypass_cooldown: True = Cooldown ignorieren (fuer
            Recovery-Nachrichten z.B. "wieder online").

    Returns:
        True wenn der Alert zugestellt wurde, sonst False. Erfolg-False
        heisst NICHT, dass der Caller einen Retry machen sollte —
        "fire & forget".
    """
    # Unter pytest wird NICHTS zugestellt und nichts protokolliert.
    # Anlass: `tests/test_phase_b_modules.py` rief den ORS-Quota-Alarm
    # echt auf; seit Mai standen dadurch 139 Phantom-Alarme im
    # admin_audit_log der PRODUKTIV-Datenbank — die Suite laeuft im
    # Container gegen die echte DB. Solange der Alarmweg tot war, fiel es
    # nicht auf; seit er wieder zustellt, wuerde jeder Testlauf eine
    # Systemwarnung ausloesen. Ein Alarmweg, den Tests bedienen koennen,
    # ist keiner.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        logger.debug("Sven-Alert '%s' im Test unterdrueckt.", kind)
        return False

    if not bypass_cooldown:
        if await _was_admin_recently_alerted(
            kind=kind, cooldown_hours=cooldown_hours,
        ):
            logger.info(
                f"Sven-Alert '{kind}' unterdrueckt (cooldown {cooldown_hours}h)"
            )
            return False

    sent = await _deliver_to_sven(kind, message)
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
