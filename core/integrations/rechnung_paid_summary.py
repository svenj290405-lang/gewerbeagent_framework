"""Background-Task: Tages-Zusammenfassung der bezahlten Rechnungen.

Wird in core/api/app.py via asyncio.create_task() gestartet.

Was es tut:
- Einmal pro Kalendertag (Europe/Berlin) gegen 18:00 lokal aufwachen
- Pro Tenant: alle Rechnungen finden mit
    bezahlt_am::date = heute UND paid_notification_sent = false
- Wenn N>0: einen Telegram-Push an tenant.telegram_chat_id senden
  ("Heute bezahlt: 3 Rechnungen, gesamt 1.450 EUR")
- Danach paid_notification_sent=true setzen, damit der naechste Lauf
  (auch nach Container-Restart) keine Doppel-Pushes macht

Was es nicht tut:
- Es bezeichnet KEINE Rechnungen als bezahlt — das macht
  rechnung_payment_monitor.py via Lexware-Polling.
- Es gibt KEINE Web-UI dafuer; Tenants sehen die Liste in
  /rechnungen_anzeigen.

Robust gegen:
- Container-Restarts (Loop wacht jede Minute kurz auf, prueft ob die
  18:00-Marke heute schon abgearbeitet wurde — siehe last_run-Marker
  in admin_audit_log oder einfacher: paid_notification_sent als Marker)
- Tenant ohne telegram_chat_id (silent skip)
- Telegram-Versand fehlgeschlagen (nicht als gesendet markieren →
  beim naechsten Lauf nochmal probieren)
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select, update

from core.database import AsyncSessionLocal
from core.models import Tenant
from core.models.rechnung import Rechnung

logger = logging.getLogger(__name__)

# Sven-Wahl: Tages-Zusammenfassung um 18:00 Europe/Berlin.
SUMMARY_HOUR_LOCAL = 18
SUMMARY_MINUTE_LOCAL = 0
LOCAL_TZ = ZoneInfo("Europe/Berlin")

INITIAL_DELAY_SECONDS = 120  # nach App-Start etwas warten
TICK_SECONDS = 60            # einmal pro Minute prueft der Loop ob's 18:00 ist


def _format_eur(value: Decimal | None) -> str:
    """1450.00 -> '1.450,00'  (deutsche Schreibweise)"""
    if value is None:
        return "0,00"
    n = Decimal(value).quantize(Decimal("0.01"))
    parts = f"{n:,.2f}".split(".")
    parts[0] = parts[0].replace(",", ".")
    return f"{parts[0]},{parts[1]}"


def _build_summary_text(rows: list) -> str:
    """rows ist Liste von (kunde_name, betrag_brutto_eur)."""
    n = len(rows)
    total = sum((r[1] or Decimal("0") for r in rows), Decimal("0"))
    msg = f"💰 <b>Heute bezahlt:</b> {n} "
    msg += "Rechnung\n" if n == 1 else "Rechnungen\n"
    msg += f"Gesamt: <b>{_format_eur(total)} €</b>\n\n"
    # Bis zu 10 Rechnungen einzeln zeigen, dann "+ N weitere"
    for kunde, betrag in rows[:10]:
        kunde_str = (kunde or "ohne Namen")[:30]
        msg += f"  • {kunde_str} — {_format_eur(betrag)} €\n"
    if n > 10:
        msg += f"  + {n - 10} weitere\n"
    return msg.strip()


async def _send_telegram_to_tenant(tenant_chat_id, text) -> bool:
    """Schickt eine Nachricht an einen Tenant-Chat. False bei Fehler.

    Wir importieren _send_to_chat aus dem telegram_notify-Plugin lazy,
    weil core/integrations/* importiert sonst plugins/* (== Layering-
    Verletzung). Lazy-Import nur in dieser Funktion ist OK.
    """
    try:
        from plugins.telegram_notify.handler import _send_to_chat
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Telegram-Notify-Plugin nicht ladbar: {exc}")
        return False
    try:
        ok = await _send_to_chat(tenant_chat_id, text)
        return bool(ok)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Telegram-Send fehlgeschlagen: {exc}")
        return False


async def send_summary_for_tenant(
    tenant_id,
    target_date: date,
) -> dict:
    """Sendet die Tages-Zusammenfassung fuer einen einzelnen Tenant.

    target_date ist das lokale Datum (Europe/Berlin), an dem bezahlt
    worden sein muss damit der Eintrag eingeschlossen wird.

    Returns: {sent: bool, count: int, total_eur: Decimal, skipped: str|None}
    """
    summary = {
        "sent": False, "count": 0,
        "total_eur": Decimal("0"), "skipped": None,
    }

    # Ein Tag in UTC: dezent um die Zeitzone normalisieren
    day_start_local = datetime.combine(target_date, time.min, tzinfo=LOCAL_TZ)
    day_end_local = day_start_local + timedelta(days=1)
    day_start_utc = day_start_local.astimezone(timezone.utc)
    day_end_utc = day_end_local.astimezone(timezone.utc)

    async with AsyncSessionLocal() as session:
        tenant = (await session.execute(
            select(Tenant).where(Tenant.id == tenant_id)
        )).scalar_one_or_none()
        if tenant is None:
            summary["skipped"] = "tenant-not-found"
            return summary
        if not tenant.telegram_chat_id:
            summary["skipped"] = "no-telegram-chat"
            return summary

        rows = (await session.execute(
            select(
                Rechnung.id,
                Rechnung.kunde_name,
                Rechnung.betrag_brutto_eur,
            ).where(
                Rechnung.tenant_id == tenant_id,
                Rechnung.bezahlt_am.is_not(None),
                Rechnung.bezahlt_am >= day_start_utc,
                Rechnung.bezahlt_am < day_end_utc,
                Rechnung.paid_notification_sent.is_(False),
            )
        )).all()

    if not rows:
        summary["skipped"] = "nothing-paid-today"
        return summary

    payload = [(r[1], r[2]) for r in rows]
    summary["count"] = len(rows)
    summary["total_eur"] = sum(
        (r[2] or Decimal("0") for r in rows), Decimal("0"),
    )

    text = _build_summary_text(payload)
    sent_ok = await _send_telegram_to_tenant(
        tenant.telegram_chat_id, text,
    )

    if not sent_ok:
        summary["skipped"] = "telegram-failed"
        # paid_notification_sent NICHT setzen → naechster Lauf probiert es
        return summary

    # Markieren — auch wenn der naechste Polling-Lauf die selbe Rechnung
    # nochmal sieht, wird sie nicht doppelt gepusht.
    rechnung_ids = [r[0] for r in rows]
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(Rechnung)
            .where(Rechnung.id.in_(rechnung_ids))
            .values(paid_notification_sent=True)
        )
        await session.commit()

    summary["sent"] = True
    return summary


async def run_summary_for_today() -> dict:
    """Ein Lauf: alle Tenants checken, jeweils Tages-Zusammenfassung schicken."""
    today_local = datetime.now(LOCAL_TZ).date()
    out = {
        "tenants": 0, "sent": 0, "skipped": 0, "rechnungen": 0,
    }

    # Wir wollen NUR Tenants behandeln die ueberhaupt heute bezahlte
    # Rechnungen haben — sonst wuerden wir bei 1000 Tenants 1000 SELECTs
    # abfeuern. Erstmal grob filtern:
    day_start_local = datetime.combine(today_local, time.min, tzinfo=LOCAL_TZ)
    day_end_local = day_start_local + timedelta(days=1)
    async with AsyncSessionLocal() as session:
        candidate_tenant_ids = (await session.execute(
            select(Rechnung.tenant_id).distinct().where(
                Rechnung.bezahlt_am.is_not(None),
                Rechnung.bezahlt_am >= day_start_local.astimezone(timezone.utc),
                Rechnung.bezahlt_am < day_end_local.astimezone(timezone.utc),
                Rechnung.paid_notification_sent.is_(False),
            )
        )).scalars().all()

    for tid in candidate_tenant_ids:
        out["tenants"] += 1
        try:
            res = await send_summary_for_tenant(tid, today_local)
            if res["sent"]:
                out["sent"] += 1
                out["rechnungen"] += res["count"]
            else:
                out["skipped"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                f"Tages-Zusammenfassung Tenant {tid} crashed: {exc}"
            )
            out["skipped"] += 1
        await asyncio.sleep(0.2)  # nicht alle gleichzeitig pushen

    return out


def _next_run_at(now_local: datetime) -> datetime:
    """Naechster Trigger-Zeitpunkt: heute 18:00 falls noch nicht durch,
    sonst morgen 18:00."""
    today_run = now_local.replace(
        hour=SUMMARY_HOUR_LOCAL, minute=SUMMARY_MINUTE_LOCAL,
        second=0, microsecond=0,
    )
    if now_local < today_run:
        return today_run
    return today_run + timedelta(days=1)


async def cron_loop() -> None:
    """Endlosschleife: jede Minute prufen, ob die 18:00-Marke ueberschritten
    wurde und der heutige Lauf noch aussteht."""
    logger.info(
        f"Bezahl-Tages-Zusammenfassung-Cron gestartet "
        f"(taeglich {SUMMARY_HOUR_LOCAL:02d}:"
        f"{SUMMARY_MINUTE_LOCAL:02d} Europe/Berlin)"
    )
    await asyncio.sleep(INITIAL_DELAY_SECONDS)

    last_run_date: date | None = None

    while True:
        try:
            now_local = datetime.now(LOCAL_TZ)
            today = now_local.date()

            # Wir sind durch wenn: heutiges Datum noch nicht abgehakt
            # UND wir sind nach 18:00 lokal.
            should_run = (
                last_run_date != today
                and (
                    now_local.hour > SUMMARY_HOUR_LOCAL
                    or (
                        now_local.hour == SUMMARY_HOUR_LOCAL
                        and now_local.minute >= SUMMARY_MINUTE_LOCAL
                    )
                )
            )

            if should_run:
                logger.info("Tages-Zusammenfassung Lauf wird ausgefuehrt")
                summary = await run_summary_for_today()
                logger.info(
                    f"Tages-Zusammenfassung: {summary['sent']} Tenants "
                    f"benachrichtigt, {summary['rechnungen']} Rechnungen "
                    f"insgesamt, {summary['skipped']} skipped"
                )
                last_run_date = today

            await asyncio.sleep(TICK_SECONDS)
        except asyncio.CancelledError:
            logger.info("Tages-Zusammenfassung-Cron gestoppt")
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                f"Tages-Zusammenfassung-Loop unerwarteter Fehler: {exc}"
            )
            await asyncio.sleep(TICK_SECONDS)
