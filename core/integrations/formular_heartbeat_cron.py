"""Daily-Heartbeat: pingt morgens offene Formulare an.

Damit Formulare nicht unter den Tisch fallen: wenn am Morgen offene
Antworten (status 'neu' / 'in_bearbeitung') aelter als 12h liegen,
bekommt der Tenant einen kurzen Telegram-Push.

Trigger: 09:00 Europe/Berlin, einmal pro Tag, idempotent ueber
last_run_date-Marker in-process (vergleichbar mit
rechnung_paid_summary).

Was es bewusst NICHT macht:
- Es schickt nicht die Antworten erneut — der User soll /formulare_offen
  oder /formular_eingang_<id> nutzen um Details zu sehen. Der Heartbeat
  ist nur ein Stupser.
- Es eskaliert nicht (kein zweiter Ping nach N Tagen). Das wuerde nur
  Notification-Fatigue erzeugen; lieber stillschweigend weitermachen.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from zoneinfo import ZoneInfo

from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.models import Tenant

logger = logging.getLogger(__name__)

PING_HOUR_LOCAL = 9
PING_MINUTE_LOCAL = 0
LOCAL_TZ = ZoneInfo("Europe/Berlin")

OPEN_THRESHOLD = dt.timedelta(hours=12)

INITIAL_DELAY_SECONDS = 120
TICK_SECONDS = 60


def _build_ping_text(count: int) -> str:
    if count == 1:
        body = "Du hast <b>1 offenes Formular</b> seit gestern."
    else:
        body = f"Du hast <b>{count} offene Formulare</b> seit gestern."
    return (
        f"📋 {body}\n\n"
        "<i>/formulare_offen zeigt die Liste.</i>"
    )


async def _send_to_tenant_chat(tenant_chat_id, text) -> bool:
    """Lazy-Import wegen Layering — plugins/* darf core/* nicht direkt
    rein, aber umgekehrt geht es nur via Lazy-Import (gleicher Trick
    wie in rechnung_paid_summary)."""
    try:
        from plugins.telegram_notify.handler import _send_to_chat
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"telegram_notify nicht ladbar: {exc}")
        return False
    try:
        return bool(await _send_to_chat(tenant_chat_id, text))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"_send_to_chat failed: {exc}")
        return False


async def run_heartbeat_once() -> dict[str, int]:
    """Eine Runde: alle Tenants mit offenen Anfragen > OPEN_THRESHOLD
    pingen. Returns {'tenants_pinged': N, 'tenants_skipped': M}.

    Skipped wird ein Tenant nur wenn er keinerlei telegram_chat_id hat —
    dann gibt es kein Push-Ziel. Multi-Mitarbeiter-Routing wird nicht
    gemacht: der Heartbeat geht immer an den Inhaber-Chat. Die einzelnen
    offenen Anfragen wandern weiter ueber das Skill-Routing.
    """
    from core.integrations.formular_eingang import find_tenants_with_overdue

    overdue = await find_tenants_with_overdue(older_than=OPEN_THRESHOLD)
    if not overdue:
        return {"tenants_pinged": 0, "tenants_skipped": 0}

    pinged, skipped = 0, 0
    async with AsyncSessionLocal() as session:
        tenants = (await session.execute(
            select(Tenant).where(Tenant.id.in_(overdue.keys()))
        )).scalars().all()

    for tenant in tenants:
        chat_id = getattr(tenant, "telegram_chat_id", None)
        if not chat_id:
            skipped += 1
            continue
        text = _build_ping_text(overdue[tenant.id])
        ok = await _send_to_tenant_chat(chat_id, text)
        if ok:
            pinged += 1
            logger.info(
                f"Formular-Heartbeat: Tenant {tenant.slug} gepingt "
                f"({overdue[tenant.id]} offen)"
            )
        else:
            skipped += 1
    return {"tenants_pinged": pinged, "tenants_skipped": skipped}


def _should_trigger(now_local: dt.datetime, last_run_date: dt.date | None) -> bool:
    """True wenn der heutige Lauf noch aussteht UND wir nach 09:00 sind."""
    today = now_local.date()
    if last_run_date == today:
        return False
    if now_local.hour > PING_HOUR_LOCAL:
        return True
    if now_local.hour == PING_HOUR_LOCAL and now_local.minute >= PING_MINUTE_LOCAL:
        return True
    return False


async def cron_loop() -> None:
    """Endlosschleife (Pattern identisch zu rechnung_paid_summary): jede
    Minute aufwachen, pruefen ob 09:00 lokal ueberschritten und heute
    noch nicht durch."""
    logger.info(
        f"Formular-Heartbeat-Cron gestartet "
        f"(taeglich {PING_HOUR_LOCAL:02d}:{PING_MINUTE_LOCAL:02d} Europe/Berlin)"
    )
    await asyncio.sleep(INITIAL_DELAY_SECONDS)

    last_run_date: dt.date | None = None
    from core.integrations.cron_health import record_heartbeat

    while True:
        try:
            now_local = dt.datetime.now(LOCAL_TZ)
            if _should_trigger(now_local, last_run_date):
                logger.info("Formular-Heartbeat-Lauf wird ausgefuehrt")
                summary = await run_heartbeat_once()
                logger.info(
                    f"Formular-Heartbeat: {summary['tenants_pinged']} gepingt, "
                    f"{summary['tenants_skipped']} skipped"
                )
                last_run_date = now_local.date()

            record_heartbeat("formular_heartbeat")
            await asyncio.sleep(TICK_SECONDS)
        except asyncio.CancelledError:
            logger.info("Formular-Heartbeat-Cron gestoppt")
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"Formular-Heartbeat-Cron unerwartet: {exc}")
            await asyncio.sleep(TICK_SECONDS)
