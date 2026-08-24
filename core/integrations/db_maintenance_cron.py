"""DB-Maintenance-Cron — taegliche Aufraeumarbeiten (Phase B3).

Laeuft 1x taeglich um 02:00 Europe/Berlin (idle, eine Stunde vor dem
DSGVO-Cleanup damit beide nicht konkurrieren).

Was es macht:
  1. admin_audit_log: Eintraege aelter als 180 Tage loeschen
  2. oauth_states: Orphans aelter als 7 Tage (verlassene Halb-Logins)
  3. visualisierungen: aelter als 90 Tage → image_bytes auf NULL,
     Metadaten bleiben fuer Statistik
  4. abgelaufene Sitzungen (app_sessions, admin_sessions) + 30 Tage
  5. admin_login_attempts aelter als 90 Tage
  6. api_usage_log aelter als 180 Tage

Zu 4-6 (Audit 2026-08-24): fuer diese Tabellen gab es gar keine Frist.
Abgelaufene Sitzungen tragen ``ip_address`` und ``user_agent``; 149 von
211 Zeilen waren laengst tot, die aelteste knapp drei Monate. Das mit
"Speicherbegrenzung" (Art. 5 Abs. 1 lit. e) zu begruenden, faellt schwer.

Wichtige Design-Entscheidung: KEIN Hard-Delete der Visualisierungen,
nur das fette image_data wird NULL gesetzt. Dadurch:
  - DB-Wachstum bleibt im Griff (ein Render ist ~2-5MB Base64)
  - /visualisierung-Statistik im Admin bleibt korrekt
  - Tenant sieht "Bild geloescht (>90 Tage)" statt einer 404

Failsafe-Pattern: jeder Schritt ist in try-except gewrapt, ein
fehlender Schritt blockiert nicht die anderen.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import zoneinfo

from sqlalchemy import delete, update

from core.database import AsyncSessionLocal

logger = logging.getLogger(__name__)


# 02:00 Berlin — eine Stunde vor DSGVO-Cleanup (03:00) damit es keinen
# Lock-Konflikt auf grossen Tabellen gibt.
MAINTENANCE_HOUR_LOCAL = 2
TICK_INTERVAL_SECONDS = 60

# Retention-Konstanten — koennten spaeter konfigurierbar werden.
AUDIT_LOG_RETENTION_DAYS = 180
OAUTH_STATE_RETENTION_DAYS = 7
VISUALISIERUNG_BLOB_RETENTION_DAYS = 90
# Nachlauf, bis eine abgelaufene Sitzung geloescht wird. Nicht sofort: bei
# einem Streit ("wer war das?") ist die Spur ein paar Wochen wert, danach
# ist sie nur noch ein Datensatz zu einer Person.
SESSION_NACHLAUF_TAGE = 30
LOGIN_ATTEMPT_RETENTION_DAYS = 90
API_USAGE_RETENTION_DAYS = 180

_last_run_date: dt.date | None = None


async def _cleanup_audit_log() -> int:
    """admin_audit_log > 180 Tage loeschen."""
    from core.models.admin import AdminAuditLog
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        days=AUDIT_LOG_RETENTION_DAYS,
    )
    async with AsyncSessionLocal() as s:
        result = await s.execute(
            delete(AdminAuditLog).where(AdminAuditLog.created_at < cutoff)
        )
        await s.commit()
        return result.rowcount or 0


async def _cleanup_oauth_states() -> int:
    """oauth_states > 7 Tage = sicher orphan."""
    from core.models import OAuthState
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        days=OAUTH_STATE_RETENTION_DAYS,
    )
    async with AsyncSessionLocal() as s:
        result = await s.execute(
            delete(OAuthState).where(OAuthState.created_at < cutoff)
        )
        await s.commit()
        return result.rowcount or 0


async def _cleanup_visualisierung_blobs() -> int:
    """Image-Bytes aelter als 90 Tage NULL setzen (Metadaten bleiben)."""
    from core.models import Visualisierung
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        days=VISUALISIERUNG_BLOB_RETENTION_DAYS,
    )
    async with AsyncSessionLocal() as s:
        # Wir wollen nur die Zeilen, die noch image_data haben — sonst
        # zaehlen wir dauerhaft alle alten Eintraege jeden Tag.
        result = await s.execute(
            update(Visualisierung)
            .where(Visualisierung.created_at < cutoff)
            .where(
                (Visualisierung.original_image_data.is_not(None))
                | (Visualisierung.result_image_data.is_not(None))
            )
            .values(
                original_image_data=None,
                result_image_data=None,
                updated_at=dt.datetime.now(dt.timezone.utc),
            )
        )
        await s.commit()
        return result.rowcount or 0


async def _cleanup_abgelaufene_sitzungen() -> int:
    """Abgelaufene PWA- und Admin-Sitzungen mit Nachlauf loeschen.

    Beide Tabellen tragen IP und User-Agent. Eine Sitzung, die seit einem
    Monat abgelaufen ist, hat keinen Zweck mehr — auch keinen technischen:
    die Auth-Pfade pruefen ``expires_at`` ohnehin.
    """
    from core.models.admin import AdminSession
    from core.models.app_account import AppSession

    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        days=SESSION_NACHLAUF_TAGE,
    )
    geloescht = 0
    async with AsyncSessionLocal() as s:
        for modell in (AppSession, AdminSession):
            result = await s.execute(
                delete(modell).where(modell.expires_at < cutoff)
            )
            geloescht += result.rowcount or 0
        await s.commit()
    return geloescht


async def _cleanup_login_versuche() -> int:
    """admin_login_attempts > 90 Tage. Der Puffer dient dem Rate-Limit
    ueber Minuten — alles darueber ist eine IP-Liste ohne Zweck."""
    from core.models.admin import AdminLoginAttempt

    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        days=LOGIN_ATTEMPT_RETENTION_DAYS,
    )
    async with AsyncSessionLocal() as s:
        result = await s.execute(
            delete(AdminLoginAttempt).where(AdminLoginAttempt.attempted_at < cutoff)
        )
        await s.commit()
        return result.rowcount or 0


async def _cleanup_api_usage_log() -> int:
    """api_usage_log > 180 Tage. Kein Personenbezug, aber unbegrenztes
    Wachstum — und die Kostenauswertung schaut hoechstens ein Jahr zurueck."""
    from core.models.admin import ApiUsageLog

    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        days=API_USAGE_RETENTION_DAYS,
    )
    async with AsyncSessionLocal() as s:
        result = await s.execute(
            delete(ApiUsageLog).where(ApiUsageLog.created_at < cutoff)
        )
        await s.commit()
        return result.rowcount or 0


async def _run_maintenance_once() -> dict:
    """Fuehrt alle Maintenance-Schritte aus, sammelt Statistik."""
    summary: dict = {}
    for label, fn in (
        ("audit_log_deleted", _cleanup_audit_log),
        ("oauth_states_deleted", _cleanup_oauth_states),
        ("visualisierungen_blob_nulled", _cleanup_visualisierung_blobs),
        ("sessions_deleted", _cleanup_abgelaufene_sitzungen),
        ("login_attempts_deleted", _cleanup_login_versuche),
        ("api_usage_deleted", _cleanup_api_usage_log),
    ):
        try:
            summary[label] = await fn()
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"DB-Maintenance step {label} crashed: {exc}")
            summary[label] = f"ERROR: {exc}"
    return summary


async def _maybe_run() -> None:
    global _last_run_date
    berlin = zoneinfo.ZoneInfo("Europe/Berlin")
    now_local = dt.datetime.now(berlin)
    today = now_local.date()
    if _last_run_date == today:
        return
    if now_local.hour < MAINTENANCE_HOUR_LOCAL:
        return

    logger.info(
        f"DB-Maintenance startet (date={today.isoformat()})"
    )
    summary = await _run_maintenance_once()
    logger.info(f"DB-Maintenance fertig: {summary}")
    _last_run_date = today


async def cron_loop() -> None:
    """Backgroundtask: tick alle 60s, fuehre Maintenance um 02:00 aus."""
    logger.info(
        f"DB-Maintenance-Cron gestartet "
        f"(taegl. {MAINTENANCE_HOUR_LOCAL:02d}:00 Europe/Berlin)"
    )
    from core.integrations.cron_health import record_heartbeat
    try:
        while True:
            try:
                await _maybe_run()
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"db_maintenance tick crashed: {exc}")
            record_heartbeat("db_maintenance_cron")
            await asyncio.sleep(TICK_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        logger.info("DB-Maintenance-Cron gestoppt")
        raise
