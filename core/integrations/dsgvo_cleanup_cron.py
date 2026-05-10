"""DSGVO-Cleanup-Cron — laeuft 1x taeglich um 03:00 Europe/Berlin.

Loescht Mail-Konversationen deren Termin laenger als 14 Tage zurueck-
liegt (Datenminimierung). Nutzt die bestehende cleanup()-Funktion aus
scripts/cleanup_email_conversations.py — keine Code-Duplikation.

Patterns analog zu rechnung_paid_summary_cron:
- Tickt jede Minute, prueft ob die taegliche 03:00-Marke schon
  abgearbeitet ist
- last_run_date-Memoiz im Prozess; bei Container-Restart vor 03:00
  laeuft heute trotzdem nur einmal
- Bei Cleanup-Fehler: log, last_run_date NICHT setzen → naechster
  Minuten-Tick versucht es nochmal (Backoff implizit durch Tick-
  Frequenz)

Aktivierungs-Status: nach commit 6f4f735 in core/api/app.py-lifespan
als asyncio.create_task() gestartet — laeuft seitdem automatisch.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import zoneinfo

logger = logging.getLogger(__name__)

# Cleanup-Zeit: 03:00 Europe/Berlin (idle-Zeit, kein Userland-Traffic)
CLEANUP_HOUR_LOCAL = 3
RETENTION_DAYS = 14
TICK_INTERVAL_SECONDS = 60

_last_run_date: dt.date | None = None


async def _maybe_run_cleanup() -> None:
    global _last_run_date
    berlin = zoneinfo.ZoneInfo("Europe/Berlin")
    now_local = dt.datetime.now(berlin)
    today = now_local.date()

    # Schon gelaufen heute? → skip.
    if _last_run_date == today:
        return
    # Marke noch nicht erreicht? → warten.
    if now_local.hour < CLEANUP_HOUR_LOCAL:
        return

    logger.info(
        f"DSGVO-Cleanup laeuft (retention={RETENTION_DAYS}d, "
        f"date={today.isoformat()})"
    )
    try:
        # cleanup() liegt im scripts-Modul — importieren on-demand
        # damit der Cron-Loop unabhaengig vom CLI-Entrypoint ist.
        from scripts.cleanup_email_conversations import cleanup
        deleted = await cleanup(RETENTION_DAYS, execute=True)
        logger.info(f"DSGVO-Cleanup fertig: {deleted} Konversationen geloescht")
        # Nur bei Erfolg merken — bei Fehler retried der naechste Tick
        _last_run_date = today
    except Exception as e:
        logger.exception(f"DSGVO-Cleanup fehlgeschlagen: {e}")


async def cron_loop() -> None:
    """Backgroundtask: tick alle 60s, fuehre Cleanup um ~03:00 aus."""
    logger.info(
        f"DSGVO-Cleanup-Cron gestartet "
        f"(taegl. {CLEANUP_HOUR_LOCAL:02d}:00 Europe/Berlin, "
        f"retention {RETENTION_DAYS}d)"
    )
    try:
        while True:
            try:
                await _maybe_run_cleanup()
            except Exception as e:  # noqa: BLE001
                logger.exception(f"Cleanup-Tick crashed: {e}")
            await asyncio.sleep(TICK_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        logger.info("DSGVO-Cleanup-Cron gestoppt")
        raise
