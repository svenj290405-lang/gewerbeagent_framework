"""
Cron-Health-Monitoring.

Jeder Background-Cron schreibt nach jedem erfolgreichen Tick einen
Heartbeat. Der Admin-Health-Endpoint kann dann pruefen ob alle Crons
noch leben.

Der Heartbeat liegt im Prozess (schnell, ohne DB-Last) UND einmal pro
Minute in der Tabelle `cron_heartbeats`. Die Persistenz kam mit dem
Audit am 2026-08-23 dazu: vorher sah nach jedem Neustart alles tot aus,
und jede Pruefung von ausserhalb des Prozesses meldete "alle Crons tot" —
solange der Alarmweg stumm war, fiel das nicht auf.
"""
from __future__ import annotations

import datetime as dt
import logging
from threading import Lock

logger = logging.getLogger(__name__)


# In-Memory: cron_name -> last_heartbeat (utc)
_HEARTBEATS: dict[str, dt.datetime] = {}
_LOCK = Lock()


# Erwartete Cron-Namen + max Toleranz in Minuten ohne Heartbeat
EXPECTED_CRONS = {
    "microsoft_cron": 5,           # Tick alle 2min, Toleranz 5
    "rechnung_payment_monitor": 35, # Tick alle 30min, Toleranz 35
    "rechnung_paid_summary": 5,    # Tick jede Minute, Toleranz 5
    "dsgvo_cleanup": 5,            # Tick jede Minute (wartet bis 03:00)
    "mail_retry_cron": 10,         # Tick alle 5min, Toleranz 10
    "db_maintenance_cron": 5,      # Tick jede Minute (wartet bis 02:00)
    "daily_health_check": 5,       # Tick jede Minute (wartet bis morgens)
    "absence_redistribution": 5,   # Tick alle 60s, Toleranz 5
    "anfrage_reminder": 70,        # Tick stuendlich, Toleranz 70min
}


# Wann zuletzt in die DB geschrieben wurde (pro Cron). Der Speicher-
# Heartbeat ist gratis, ein DB-Schreibvorgang nicht — bei einem Tick pro
# Sekunde waere das sinnlose Last.
_DB_INTERVALL_SEKUNDEN = 60
_LETZTER_DB_SCHREIB: dict[str, dt.datetime] = {}


def record_heartbeat(cron_name: str) -> None:
    """Vom Cron-Loop nach jedem Tick (oder Sleep) aufrufen.

    Schreibt sofort in den Speicher und hoechstens einmal pro Minute
    zusaetzlich in die DB — nebenlaeufig, damit ein langsamer oder
    kaputter DB-Schreibvorgang niemals einen Cron aufhaelt.
    """
    jetzt = dt.datetime.now(dt.timezone.utc)
    with _LOCK:
        _HEARTBEATS[cron_name] = jetzt
        zuletzt = _LETZTER_DB_SCHREIB.get(cron_name)
        faellig = (
            zuletzt is None
            or (jetzt - zuletzt).total_seconds() >= _DB_INTERVALL_SEKUNDEN
        )
        if faellig:
            _LETZTER_DB_SCHREIB[cron_name] = jetzt
    if not faellig:
        return
    try:
        import asyncio
        asyncio.get_running_loop().create_task(_schreibe_heartbeat(cron_name, jetzt))
    except RuntimeError:
        pass  # kein laufender Loop (Test, Sync-Kontext) — Speicher genuegt


async def _schreibe_heartbeat(cron_name: str, zeitpunkt: dt.datetime) -> None:
    """Upsert einer Zeile. Fehler werden geschluckt: ein fehlender
    Heartbeat darf den Cron nicht stoeren, den er beschreibt."""
    try:
        from sqlalchemy.dialects.postgresql import insert
        from core.database import AsyncSessionLocal
        from core.models import CronHeartbeat

        async with AsyncSessionLocal() as s:
            stmt = insert(CronHeartbeat.__table__).values(
                cron_name=cron_name, last_beat=zeitpunkt,
            ).on_conflict_do_update(
                index_elements=["cron_name"],
                set_={"last_beat": zeitpunkt},
            )
            await s.execute(stmt)
            await s.commit()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Heartbeat nicht persistiert (%s): %s", cron_name, exc)


async def lade_heartbeats_aus_db() -> dict[str, dt.datetime]:
    """Heartbeats aus der DB — fuer Pruefungen ausserhalb des Prozesses."""
    from sqlalchemy import select
    from core.database import AsyncSessionLocal
    from core.models import CronHeartbeat

    async with AsyncSessionLocal() as s:
        zeilen = (await s.execute(select(CronHeartbeat))).scalars().all()
        return {z.cron_name: z.last_beat for z in zeilen}


async def get_health_report_persistent() -> dict:
    """Wie get_health_report, aber mit den Werten aus der DB gemischt.

    Genommen wird jeweils der juengere Zeitpunkt: im laufenden Prozess ist
    der Speicherwert aktueller, von aussen gibt es nur die DB.
    """
    aus_db = await lade_heartbeats_aus_db()
    with _LOCK:
        for name, zeit in _HEARTBEATS.items():
            if name not in aus_db or zeit > aus_db[name]:
                aus_db[name] = zeit
    return get_health_report(snapshot=aus_db)


def get_health_report(snapshot: dict | None = None) -> dict:
    """Liefert Status pro Cron + globalen Status.

    Returns:
        {
            "status": "ok" | "degraded",
            "crons": {
                "microsoft_cron": {"alive": True, "minutes_since": 0.5, "last": "..."},
                ...
            }
        }
    """
    now = dt.datetime.now(dt.timezone.utc)
    report = {"status": "ok", "crons": {}}
    if snapshot is None:
        with _LOCK:
            snapshot = dict(_HEARTBEATS)

    for name, max_minutes in EXPECTED_CRONS.items():
        last = snapshot.get(name)
        if last is None:
            # Noch kein Heartbeat — Container vielleicht gerade gestartet.
            # Nach 10min mit nichts: degraded.
            report["crons"][name] = {
                "alive": False,
                "minutes_since": None,
                "last": None,
                "reason": "kein heartbeat seit start",
            }
            report["status"] = "degraded"
            continue
        delta_min = (now - last).total_seconds() / 60
        alive = delta_min <= max_minutes
        report["crons"][name] = {
            "alive": alive,
            "minutes_since": round(delta_min, 1),
            "last": last.isoformat(),
            "max_minutes": max_minutes,
        }
        if not alive:
            report["status"] = "degraded"

    return report
