"""DSGVO-Cleanup-Cron — laeuft 1x taeglich um 03:00 Europe/Berlin.

Loescht personenbezogene Daten nach Ablauf der Aufbewahrungsfrist des
jeweiligen Tenants (data_retention_days, Default 90 Tage) —
Datenminimierung / Speicherbegrenzung (Art. 5 Abs. 1 lit. e DSGVO):
- Mail-Konversationen via cleanup() aus
  scripts/cleanup_email_conversations.py
- Gespraechs-/Anruf-Transkripte + Web-Formular-Anfragen via
  scripts/cleanup_pii.py (cleanup_kundengespraeche / cleanup_anfragen)
- Geocode-Cache (Kundenadressen) global via cleanup_geocode_cache

Keine Code-Duplikation — die eigentliche Loesch-Logik liegt in den
scripts-Modulen, dieser Cron orchestriert nur pro Tenant.

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
# Default-Retention fuer Tenants die noch keinen eigenen Wert haben
# (bei NULL/0 in der Spalte). Migration setzt server_default=90, also
# sollte das hier nie greifen — defensiv aber.
DEFAULT_RETENTION_DAYS = 90
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

    logger.info(f"DSGVO-Cleanup-Lauf startet (date={today.isoformat()})")
    try:
        # Phase B4: pro Tenant mit dessen data_retention_days.
        # cleanup() liegt im scripts-Modul — importieren on-demand damit
        # der Cron-Loop unabhaengig vom CLI-Entrypoint ist.
        from sqlalchemy import select
        from core.database import AsyncSessionLocal
        from core.models import Tenant
        from scripts.cleanup_email_conversations import cleanup
        from scripts.cleanup_pii import (
            cleanup_anfragen,
            cleanup_geocode_cache,
            cleanup_kundengespraeche,
        )

        async with AsyncSessionLocal() as s:
            tenants = (await s.execute(
                select(
                    Tenant.id, Tenant.slug, Tenant.data_retention_days,
                )
            )).all()

        total_mails = 0
        total_gespraeche = 0
        total_anfragen = 0
        # Laengste Retention bestimmt, wie lange der globale Geocode-Cache
        # gehalten wird (kein Eintrag soll geloescht werden, solange noch
        # irgendein Tenant ihn innerhalb seiner Frist nutzen darf).
        max_retention = DEFAULT_RETENTION_DAYS
        for t_id, slug, retention in tenants:
            r_days = retention or DEFAULT_RETENTION_DAYS
            max_retention = max(max_retention, r_days)
            try:
                mails = await cleanup(r_days, execute=True, tenant_id=t_id)
                gespraeche = await cleanup_kundengespraeche(
                    r_days, execute=True, tenant_id=t_id
                )
                anfragen = await cleanup_anfragen(
                    r_days, execute=True, tenant_id=t_id
                )
                if mails or gespraeche or anfragen:
                    logger.info(
                        f"  tenant={slug} retention={r_days}d: "
                        f"{mails} Konversationen, {gespraeche} Gespraeche, "
                        f"{anfragen} Anfragen geloescht"
                    )
                total_mails += mails
                total_gespraeche += gespraeche
                total_anfragen += anfragen
            except Exception as t_exc:  # noqa: BLE001
                logger.exception(
                    f"DSGVO-Cleanup Tenant {slug} fehlgeschlagen: {t_exc}"
                )

        # Geocode-Cache ist tenant-uebergreifend → einmal global mit der
        # laengsten Tenant-Retention.
        geocode_deleted = 0
        try:
            geocode_deleted = await cleanup_geocode_cache(
                max_retention, execute=True
            )
        except Exception as g_exc:  # noqa: BLE001
            logger.exception(f"Geocode-Cache-Cleanup fehlgeschlagen: {g_exc}")

        logger.info(
            f"DSGVO-Cleanup fertig ueber {len(tenants)} Tenants: "
            f"{total_mails} Konversationen, {total_gespraeche} Gespraeche, "
            f"{total_anfragen} Anfragen, {geocode_deleted} Geocode-Eintraege "
            f"geloescht"
        )
        # Nur bei Erfolg merken — bei Fehler retried der naechste Tick
        _last_run_date = today
    except Exception as e:
        logger.exception(f"DSGVO-Cleanup fehlgeschlagen: {e}")


async def cron_loop() -> None:
    """Backgroundtask: tick alle 60s, fuehre Cleanup um ~03:00 aus."""
    logger.info(
        f"DSGVO-Cleanup-Cron gestartet "
        f"(taegl. {CLEANUP_HOUR_LOCAL:02d}:00 Europe/Berlin, "
        f"per-Tenant retention via data_retention_days, "
        f"Default {DEFAULT_RETENTION_DAYS}d)"
    )
    from core.integrations.cron_health import record_heartbeat
    try:
        while True:
            try:
                await _maybe_run_cleanup()
            except Exception as e:  # noqa: BLE001
                logger.exception(f"Cleanup-Tick crashed: {e}")
            record_heartbeat("dsgvo_cleanup")
            await asyncio.sleep(TICK_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        logger.info("DSGVO-Cleanup-Cron gestoppt")
        raise
