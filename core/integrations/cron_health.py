"""
Cron-Health-Monitoring.

Jeder Background-Cron schreibt nach jedem erfolgreichen Tick einen
Heartbeat. Der Admin-Health-Endpoint kann dann pruefen ob alle Crons
noch leben.

Kein neues Schema noetig — wir nutzen ein In-Memory-Dict + aktuell
ueberlebt das Container-Restart nicht. Fuer einfaches Monitoring reicht
das aus; fuer Persistenz spaeter eine Tabelle anbauen.
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
}


def record_heartbeat(cron_name: str) -> None:
    """Vom Cron-Loop nach jedem Tick (oder Sleep) aufrufen."""
    with _LOCK:
        _HEARTBEATS[cron_name] = dt.datetime.now(dt.timezone.utc)


def get_health_report() -> dict:
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
