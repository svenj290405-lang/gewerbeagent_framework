"""Taeglicher System-Health-Check.

Laeuft 1x morgens (settings.health_check_hour, Europe/Berlin) und prueft,
ob der Bot / das System noch laeuft:
  1. DB erreichbar (SELECT 1)
  2. Telegram-Bot erreichbar (getMe auf den Admin-Bot-Token)
  3. Background-Crons leben (Heartbeats via cron_health.get_health_report)

Das Ergebnis wird in health_check_results persistiert (im Admin-Tool unter
/admin/health sichtbar). Geht etwas schief (status != ok), schickt der Check
eine Alarm-Mail an settings.health_alert_email — ueber das erprobte
_global-Outlook-Plattformpostfach (gleicher Pfad wie die Onboarding-Mail).

Failsafe: jede Teilpruefung ist gekapselt, ein Fehler blockiert die anderen
nicht; ein Versand-Fehler stoppt die Persistenz nicht.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import zoneinfo

import httpx
from sqlalchemy import select, text

from config.settings import settings
from core.database import AsyncSessionLocal

logger = logging.getLogger(__name__)

TICK_INTERVAL_SECONDS = 60
TELEGRAM_TIMEOUT = 10.0
GLOBAL_TENANT_SLUG = "_global"

# Wird vom Cron-Loop gesetzt; verhindert Mehrfach-Lauf am selben Tag.
_last_run_date: dt.date | None = None


# ---------------------------------------------------------------------
# TEILPRUEFUNGEN
# ---------------------------------------------------------------------
async def _check_db() -> tuple[bool, str | None]:
    try:
        async with AsyncSessionLocal() as s:
            await s.execute(text("SELECT 1"))
        return True, None
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:200]


async def _check_telegram() -> tuple[bool, str | None]:
    """getMe auf den Admin-Bot-Token — prueft Telegram-API-Erreichbarkeit
    + Gueltigkeit des Bot-Tokens."""
    token = settings.admin_telegram_bot_token
    if not token:
        # Kein Token konfiguriert -> nicht als Fehler werten (uebersprungen).
        return True, "kein admin_telegram_bot_token gesetzt (uebersprungen)"
    try:
        async with httpx.AsyncClient(timeout=TELEGRAM_TIMEOUT) as client:
            resp = await client.get(
                f"https://api.telegram.org/bot{token}/getMe"
            )
        data = resp.json() if resp.content else {}
        if resp.status_code == 200 and data.get("ok"):
            uname = (data.get("result") or {}).get("username")
            return True, (f"@{uname}" if uname else "ok")
        return False, f"HTTP {resp.status_code}: {str(data)[:150]}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:200]


def _check_crons() -> tuple[bool, dict]:
    from core.integrations.cron_health import get_health_report
    report = get_health_report()
    return report.get("status") == "ok", report


# ---------------------------------------------------------------------
# HAUPT-CHECK
# ---------------------------------------------------------------------
async def run_health_check(*, send_alert: bool = True):
    """Fuehrt alle Teilpruefungen aus, persistiert das Ergebnis und schickt
    bei einem Problem eine Alarm-Mail. Returns das (detached)
    HealthCheckResult."""
    from core.models import (
        HealthCheckResult, HEALTH_STATUS_OK,
        HEALTH_STATUS_DEGRADED, HEALTH_STATUS_ERROR,
    )

    db_ok, db_err = await _check_db()
    tg_ok, tg_info = await _check_telegram()
    try:
        crons_ok, cron_report = _check_crons()
    except Exception as e:  # noqa: BLE001
        crons_ok, cron_report = False, {"error": str(e)[:200]}

    if not db_ok:
        status = HEALTH_STATUS_ERROR
    elif not (tg_ok and crons_ok):
        status = HEALTH_STATUS_DEGRADED
    else:
        status = HEALTH_STATUS_OK

    detail = {
        "db": {"ok": db_ok, "error": db_err},
        "telegram": {"ok": tg_ok, "info": tg_info},
        "crons": cron_report,
    }

    alert_sent = False
    if status != HEALTH_STATUS_OK and send_alert:
        try:
            alert_sent = await _send_alert_email(status, detail)
        except Exception as e:  # noqa: BLE001
            logger.exception(f"Health-Alert-Mail fehlgeschlagen: {e}")

    async with AsyncSessionLocal() as s:
        result = HealthCheckResult(
            status=status, db_ok=db_ok, telegram_ok=tg_ok,
            crons_ok=crons_ok, detail=detail, alert_sent=alert_sent,
        )
        s.add(result)
        await s.commit()
        await s.refresh(result)
        s.expunge(result)

    logger.info(
        "Daily-Health-Check: status=%s db=%s tg=%s crons=%s alert=%s",
        status, db_ok, tg_ok, crons_ok, alert_sent,
    )
    return result


# ---------------------------------------------------------------------
# ALARM-MAIL (ueber _global-Outlook)
# ---------------------------------------------------------------------
def _build_alert_bodies(status: str, detail: dict) -> tuple[str, str]:
    db = detail.get("db", {})
    tg = detail.get("telegram", {})
    crons = detail.get("crons", {})
    dead = [
        name for name, c in (crons.get("crons") or {}).items()
        if not c.get("alive")
    ]
    stamp = dt.datetime.now(zoneinfo.ZoneInfo("Europe/Berlin")).strftime(
        "%d.%m.%Y %H:%M"
    )

    def mark(ok: bool) -> str:
        return "✅ ok" if ok else "❌ PROBLEM"

    html = (
        f"<p>Der taegliche System-Health-Check von <b>Gewerbeagent</b> hat "
        f"ein Problem gemeldet (Status: <b>{status.upper()}</b>, {stamp}).</p>"
        "<ul>"
        f"<li>Datenbank: {mark(db.get('ok'))}"
        f"{(' — ' + str(db.get('error'))) if db.get('error') else ''}</li>"
        f"<li>Telegram-Bot: {mark(tg.get('ok'))} "
        f"({tg.get('info') or ''})</li>"
        f"<li>Background-Crons: {mark(crons.get('status') == 'ok')}"
        f"{(' — tot: ' + ', '.join(dead)) if dead else ''}</li>"
        "</ul>"
        "<p style=\"color:#666;font-size:13px\">Bitte den Server / die "
        "Container pruefen (docker ps, docker logs gewerbeagent_framework). "
        "Diese Mail kommt vom automatischen Morgen-Check.</p>"
    )
    text_body = (
        f"Gewerbeagent System-Health-Check: {status.upper()} ({stamp})\n\n"
        f"- Datenbank: {mark(db.get('ok'))}"
        f"{(' - ' + str(db.get('error'))) if db.get('error') else ''}\n"
        f"- Telegram-Bot: {mark(tg.get('ok'))} ({tg.get('info') or ''})\n"
        f"- Crons: {mark(crons.get('status') == 'ok')}"
        f"{(' - tot: ' + ', '.join(dead)) if dead else ''}\n\n"
        "Bitte Server/Container pruefen."
    )
    return html, text_body


async def _send_alert_email(status: str, detail: dict) -> bool:
    """Schickt die Alarm-Mail ueber das _global-Outlook-Postfach
    (gleicher Pfad wie die Onboarding-Mail). Returns True bei Versand."""
    from core.integrations.microsoft import send_tracked_mail
    from core.models import OAuthToken, Tenant

    async with AsyncSessionLocal() as s:
        gt = (await s.execute(
            select(Tenant).where(Tenant.slug == GLOBAL_TENANT_SLUG)
        )).scalar_one_or_none()
        tok = None
        if gt is not None:
            tok = (await s.execute(
                select(OAuthToken).where(
                    OAuthToken.tenant_id == gt.id,
                    OAuthToken.provider == "microsoft",
                )
            )).scalar_one_or_none()
        if gt is None or tok is None:
            logger.error(
                "Health-Alert: kein _global-Outlook-Postfach verbunden — "
                "Alarm-Mail nicht moeglich."
            )
            return False
        gid, eid = gt.id, tok.employee_id

    html, text_body = _build_alert_bodies(status, detail)
    await send_tracked_mail(
        tenant_id=gid,
        to_email=settings.health_alert_email,
        subject=f"⚠️ Gewerbeagent Health-Check: {status.upper()}",
        body_html=html,
        employee_id=eid,
        body_text=text_body,
    )
    logger.warning(
        "Health-Alert-Mail an %s verschickt (status=%s)",
        settings.health_alert_email, status,
    )
    return True


# ---------------------------------------------------------------------
# CRON-LOOP (taegl. morgens)
# ---------------------------------------------------------------------
async def _maybe_run() -> None:
    global _last_run_date
    berlin = zoneinfo.ZoneInfo("Europe/Berlin")
    now_local = dt.datetime.now(berlin)
    today = now_local.date()
    if _last_run_date == today:
        return
    if now_local.hour < settings.health_check_hour:
        return
    logger.info("Daily-Health-Check startet (date=%s)", today.isoformat())
    await run_health_check(send_alert=True)
    _last_run_date = today


async def cron_loop() -> None:
    """Backgroundtask: tick alle 60s, fuehre Health-Check 1x morgens aus."""
    logger.info(
        "Daily-Health-Check-Cron gestartet (taegl. %02d:00 Europe/Berlin)",
        settings.health_check_hour,
    )
    from core.integrations.cron_health import record_heartbeat
    try:
        while True:
            try:
                await _maybe_run()
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"daily_health_check tick crashed: {exc}")
            record_heartbeat("daily_health_check")
            await asyncio.sleep(TICK_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        logger.info("Daily-Health-Check-Cron gestoppt")
        raise
