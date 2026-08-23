"""Taeglicher System-Health-Check.

Laeuft 1x morgens (settings.health_check_hour, Europe/Berlin) und prueft,
ob der Bot / das System noch laeuft:
  1. DB erreichbar (SELECT 1)
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

from sqlalchemy import select, text

from config.settings import settings
from core.database import AsyncSessionLocal

logger = logging.getLogger(__name__)

TICK_INTERVAL_SECONDS = 60
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


async def _check_crons() -> tuple[bool, dict]:
    """Cron-Status aus Speicher UND DB.

    Frueher nur aus dem Speicher — dadurch meldete jede Pruefung
    ausserhalb des laufenden Prozesses "alle Crons tot", und direkt nach
    einem Neustart ebenfalls.
    """
    from core.integrations.cron_health import get_health_report_persistent
    report = await get_health_report_persistent()
    return report.get("status") == "ok", report


# ---------------------------------------------------------------------
# HAUPT-CHECK
# ---------------------------------------------------------------------
async def _check_anbindungen() -> tuple[bool, dict]:
    """Prueft die externen Anbindungen jedes Tenants.

    Bis zum Audit am 2026-08-23 prueften wir nur DB und Cron-Loops.
    Genau die Teile, die von aussen wegbrechen koennen — ein abgelaufenes
    Postfach-Token, ein ungueltiger Lexware-Schluessel — fielen niemandem
    auf: die `health_check()`-Funktionen existierten, wurden aber nur per
    Knopfdruck in der App aufgerufen. Ein totes Token faellt so erst auf,
    wenn eine Kundenmail unbeantwortet bleibt.

    Bewertung bewusst milde: ein Tenant OHNE Anbindung ist kein Fehler
    (nicht jeder Betrieb nutzt jede Funktion). Gemeldet wird nur, was
    verbunden IST und nicht mehr antwortet.
    """
    from sqlalchemy import select
    from core.models import Tenant
    from core.models.employee import get_default_employee
    from core.security.oauth_token_lookup import find_oauth_token

    bericht: dict = {}
    alles_ok = True

    async with AsyncSessionLocal() as s:
        tenants = (await s.execute(select(Tenant))).scalars().all()
        for t in tenants:
            s.expunge(t)

    for tenant in tenants:
        eintrag: dict = {}
        try:
            emp = await get_default_employee(tenant.id)
        except Exception:  # noqa: BLE001
            emp = None
        emp_id = emp.id if emp else None

        for provider in ("microsoft", "google"):
            try:
                token = await find_oauth_token(tenant.id, provider, emp_id)
            except Exception as exc:  # noqa: BLE001
                eintrag[provider] = {"ok": False, "fehler": str(exc)[:120]}
                alles_ok = False
                continue
            if token is None:
                continue  # nicht verbunden — kein Mangel
            ablauf = getattr(token, "access_token_expires_at", None)
            # Der Refresh-Token ist das, was wirklich zaehlt. Microsoft
            # entwertet ihn nach 90 Tagen Untaetigkeit — deshalb zaehlen
            # wir die Tage seit der letzten Benutzung.
            zuletzt = getattr(token, "updated_at", None)
            tage_still = None
            if zuletzt is not None:
                tage_still = (
                    dt.datetime.now(dt.timezone.utc) - zuletzt
                ).days
            gefaehrdet = bool(tage_still is not None and tage_still > 60)
            eintrag[provider] = {
                "ok": not gefaehrdet,
                "ablauf": ablauf.isoformat() if ablauf else None,
                "tage_ohne_nutzung": tage_still,
            }
            if gefaehrdet:
                alles_ok = False

        # Lexware: derselbe Weg wie der "Verbindung testen"-Knopf in der
        # App, nur eben automatisch.
        try:
            from core.api.app_screens import _build_lexware_provider
            provider = await _build_lexware_provider(tenant.id)
        except Exception:  # noqa: BLE001
            provider = None
        if provider is not None:
            try:
                await provider.health_check()
                eintrag["lexware"] = {"ok": True}
            except Exception as exc:  # noqa: BLE001
                eintrag["lexware"] = {"ok": False, "fehler": str(exc)[:160]}
                alles_ok = False

        if eintrag:
            bericht[tenant.slug] = eintrag

    return alles_ok, bericht


async def run_health_check(*, send_alert: bool = True):
    """Fuehrt alle Teilpruefungen aus, persistiert das Ergebnis und schickt
    bei einem Problem eine Alarm-Mail. Returns das (detached)
    HealthCheckResult."""
    from core.models import (
        HealthCheckResult, HEALTH_STATUS_OK,
        HEALTH_STATUS_DEGRADED, HEALTH_STATUS_ERROR,
    )

    db_ok, db_err = await _check_db()
    try:
        crons_ok, cron_report = await _check_crons()
    except Exception as e:  # noqa: BLE001
        crons_ok, cron_report = False, {"error": str(e)[:200]}

    try:
        anbindungen_ok, anbindungen = await _check_anbindungen()
    except Exception as e:  # noqa: BLE001
        logger.exception(f"Anbindungs-Pruefung fehlgeschlagen: {e}")
        anbindungen_ok, anbindungen = True, {"fehler": str(e)[:200]}

    if not db_ok:
        status = HEALTH_STATUS_ERROR
    elif not crons_ok or not anbindungen_ok:
        status = HEALTH_STATUS_DEGRADED
    else:
        status = HEALTH_STATUS_OK

    detail = {
        "db": {"ok": db_ok, "error": db_err},
        "crons": cron_report,
        "anbindungen": anbindungen,
    }

    alert_sent = False
    if status != HEALTH_STATUS_OK and send_alert:
        try:
            alert_sent = await _send_alert_email(status, detail)
        except Exception as e:  # noqa: BLE001
            logger.exception(f"Health-Alert-Mail fehlgeschlagen: {e}")

    async with AsyncSessionLocal() as s:
        result = HealthCheckResult(
            status=status, db_ok=db_ok,
            crons_ok=crons_ok, detail=detail, alert_sent=alert_sent,
        )
        s.add(result)
        await s.commit()
        await s.refresh(result)
        s.expunge(result)

    logger.info(
        "Daily-Health-Check: status=%s db=%s crons=%s alert=%s",
        status, db_ok, crons_ok, alert_sent,
    )
    return result


# ---------------------------------------------------------------------
# ALARM-MAIL (ueber _global-Outlook)
# ---------------------------------------------------------------------
def _build_alert_bodies(status: str, detail: dict) -> tuple[str, str]:
    db = detail.get("db", {})
    crons = detail.get("crons", {})
    dead = [
        name for name, c in (crons.get("crons") or {}).items()
        if not c.get("alive")
    ]
    stamp = dt.datetime.now(zoneinfo.ZoneInfo("Europe/Berlin")).strftime(
        "%d.%m.%Y %H:%M"
    )

    # Auffaellige Anbindungen aufzaehlen — ein totes Postfach-Token oder
    # ein ungueltiger Lexware-Schluessel ist genau die Art Ausfall, die
    # sonst erst auffaellt, wenn eine Kundenmail unbeantwortet bleibt.
    probleme: list[str] = []
    for slug, dienste in (detail.get("anbindungen") or {}).items():
        if not isinstance(dienste, dict):
            continue
        for dienst, info in dienste.items():
            if isinstance(info, dict) and not info.get("ok"):
                grund = info.get("fehler")
                if not grund and info.get("tage_ohne_nutzung") is not None:
                    grund = (f"seit {info['tage_ohne_nutzung']} Tagen nicht "
                             f"benutzt — Token verfaellt")
                probleme.append(f"{slug}/{dienst}: {grund or 'antwortet nicht'}")

    def mark(ok: bool) -> str:
        return "✅ ok" if ok else "❌ PROBLEM"

    html = (
        f"<p>Der taegliche System-Health-Check von <b>Gewerbeagent</b> hat "
        f"ein Problem gemeldet (Status: <b>{status.upper()}</b>, {stamp}).</p>"
        "<ul>"
        f"<li>Datenbank: {mark(db.get('ok'))}"
        f"{(' — ' + str(db.get('error'))) if db.get('error') else ''}</li>"
        f"<li>Background-Crons: {mark(crons.get('status') == 'ok')}"
        f"{(' — tot: ' + ', '.join(dead)) if dead else ''}</li>"
        f"<li>Anbindungen: {mark(not probleme)}"
        f"{('<br>' + '<br>'.join(probleme)) if probleme else ''}</li>"
        "</ul>"
        "<p style=\"color:#666;font-size:13px\">Bitte den Server / die "
        "Container pruefen (docker ps, docker logs gewerbeagent_framework). "
        "Diese Mail kommt vom automatischen Morgen-Check.</p>"
    )
    text_body = (
        f"Gewerbeagent System-Health-Check: {status.upper()} ({stamp})\n\n"
        f"- Datenbank: {mark(db.get('ok'))}"
        f"{(' - ' + str(db.get('error'))) if db.get('error') else ''}\n"
        f"- Crons: {mark(crons.get('status') == 'ok')}"
        f"{(' - tot: ' + ', '.join(dead)) if dead else ''}\n"
        f"- Anbindungen: {mark(not probleme)}"
        f"{(chr(10) + '  ' + (chr(10) + '  ').join(probleme)) if probleme else ''}\n\n"
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
    global _last_run_date
    logger.info(
        "Daily-Health-Check-Cron gestartet (taegl. %02d:00 Europe/Berlin)",
        settings.health_check_hour,
    )
    # Beim Start NICHT sofort nachfeuern, wenn die Check-Stunde heute schon
    # vorbei ist — sonst loest jeder Restart (z.B. ein Deploy am Nachmittag)
    # einen Lauf aus, der wegen noch kalter Cron-Heartbeats faelschlich
    # "degraded" meldet und eine Fehlalarm-Mail schickt. Erst morgen.
    now_local = dt.datetime.now(zoneinfo.ZoneInfo("Europe/Berlin"))
    if now_local.hour >= settings.health_check_hour:
        _last_run_date = now_local.date()
        logger.info(
            "Daily-Health-Check: heutiger Lauf uebersprungen (Start nach "
            "%02d:00) — naechster Lauf morgen frueh.",
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
