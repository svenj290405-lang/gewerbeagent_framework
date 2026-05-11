"""DB-Maintenance-Cron — taegliche Aufraeumarbeiten (Phase B3).

Laeuft 1x taeglich um 02:00 Europe/Berlin (idle, eine Stunde vor dem
DSGVO-Cleanup damit beide nicht konkurrieren).

Was es macht:
  1. admin_audit_log: Eintraege aelter als 180 Tage loeschen
  2. oauth_states: Orphans aelter als 7 Tage (verlassene Halb-Logins)
  3. telegram_state: Eintraege mit expires_at < now (oder created_at
     aelter als 24h falls expires_at NULL ist — defensive Cleanup)
  4. visualisierungen: aelter als 90 Tage → image_bytes auf NULL,
     Metadaten bleiben fuer Statistik

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
TELEGRAM_STATE_FALLBACK_DAYS = 1
VISUALISIERUNG_BLOB_RETENTION_DAYS = 90

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


async def _cleanup_telegram_states() -> int:
    """Expired telegram_state-Eintraege wegraeumen.

    Bevorzugt expires_at, fallback created_at + 24h fuer NULL-Werte.
    """
    from core.models import TelegramState
    now = dt.datetime.now(dt.timezone.utc)
    cutoff_fallback = now - dt.timedelta(days=TELEGRAM_STATE_FALLBACK_DAYS)
    async with AsyncSessionLocal() as s:
        # Variante A: expires_at vorhanden und schon vorbei
        result_a = await s.execute(
            delete(TelegramState)
            .where(TelegramState.expires_at.is_not(None))
            .where(TelegramState.expires_at < now)
        )
        # Variante B: kein expires_at, dafuer alt
        result_b = await s.execute(
            delete(TelegramState)
            .where(TelegramState.expires_at.is_(None))
            .where(TelegramState.created_at < cutoff_fallback)
        )
        await s.commit()
        return (result_a.rowcount or 0) + (result_b.rowcount or 0)


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


async def _run_maintenance_once() -> dict:
    """Fuehrt alle Maintenance-Schritte aus, sammelt Statistik."""
    summary: dict = {}
    for label, fn in (
        ("audit_log_deleted", _cleanup_audit_log),
        ("oauth_states_deleted", _cleanup_oauth_states),
        ("telegram_states_deleted", _cleanup_telegram_states),
        ("visualisierungen_blob_nulled", _cleanup_visualisierung_blobs),
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
