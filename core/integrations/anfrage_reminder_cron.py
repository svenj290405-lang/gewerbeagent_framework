"""24h-Reminder-Cron fuer offene Anfrage-Formulare.

Stuendlicher Tick. Pro Tenant:
1. Alle offenen Tokens holen (kein submitted_at, kein reminder_sent_at,
   nicht expired).
2. Fuer jeden Token im Calendar nach Termin matchen (kunde_email ODER
   kunde_telefon, Fenster [now+22h, now+26h]).
3. Treffer → Reminder-Mail an Kunde + reminder_sent_at = NOW().

Fenster 22-26h statt 23-25h fuer Resilienz: wenn der Cron-Tick mal
verzoegert kommt (oder ein Termin fast zur naechsten Stunden-Grenze
liegt), kriegen wir's trotzdem. reminder_sent_at verhindert
Doppel-Mails.

Aktivierung: in core/api/app.py-lifespan als asyncio.create_task().
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid
import zoneinfo

from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.models import Tenant

logger = logging.getLogger(__name__)

TICK_INTERVAL_SECONDS = 60 * 60  # 1 Stunde
REMINDER_WINDOW_MIN_HOURS = 22
REMINDER_WINDOW_MAX_HOURS = 26
BERLIN = zoneinfo.ZoneInfo("Europe/Berlin")


def _reminder_mail_html(
    *, kunde_anrede: str, company_name: str, form_url: str,
    termin_label: str,
) -> str:
    """Knappe Erinnerungs-Mail. Subject-/Body-Tonalitaet wie
    build_kunde_reply_html (du-form, freundlich, knapp).
    """
    from html import escape as _e
    return (
        "<html><body style=\"font-family:Arial,Helvetica,sans-serif;"
        "font-size:14px;color:#222\">"
        f"<p>Hallo {_e(kunde_anrede)},</p>"
        f"<p>kurze Erinnerung: morgen ist dein Termin "
        f"({_e(termin_label)}). Damit wir gut vorbereitet sind, "
        f"fuell bitte vorher noch kurz unser Anfrage-Formular aus:</p>"
        f"<p><a href=\"{_e(form_url)}\" "
        f"style=\"display:inline-block;padding:10px 18px;"
        f"background:#2563eb;color:white;text-decoration:none;"
        f"border-radius:6px\">Formular ausfuellen</a></p>"
        f"<p>Wenn du es schon ausgefuellt hast, ignoriere diese Mail "
        f"einfach — die kreuzen sich.</p>"
        f"<p>Bis morgen,<br>{_e(company_name)}</p>"
        "</body></html>"
    )


def _termin_label(start_dt: dt.datetime) -> str:
    """'Mo 19.05. um 14:30'-artige Anzeige im Berlin-TZ."""
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=dt.timezone.utc)
    local = start_dt.astimezone(BERLIN)
    wd = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"][local.weekday()]
    return f"{wd} {local.strftime('%d.%m.')} um {local.strftime('%H:%M')}"


async def _process_tenant(tenant: Tenant) -> tuple[int, int]:
    """Returns (checked, sent).

    Calendar-Call-Dedup: wir gruppieren alle offenen Tokens nach
    (kunde_email, kunde_telefon) und machen pro Gruppe NUR EINEN
    Calendar-Lookup. Bei Treffer wird auch nur EINE Mail verschickt
    (alle Tokens der Gruppe gehoeren demselben Kunden — sonst waeren
    sie nicht in einer Gruppe). Alle Token-IDs der Gruppe werden
    danach mit reminder_sent_at markiert damit der naechste Tick
    sie nicht erneut anfasst.
    """
    from core.integrations.anfrage_status import (
        find_open_tokens_for_reminder, mark_reminder_sent,
    )
    from core.integrations.microsoft import send_mail_as_user
    from plugins.kalender.adapters import get_calendar_adapter
    from core.integrations.anfrage_forms import build_anfrage_url
    from core.integrations.mail_template import extract_first_name

    tokens = await find_open_tokens_for_reminder(tenant.id)
    if not tokens:
        return 0, 0

    # Nach (email_lower, telefon) gruppieren, jüngsten Token pro Gruppe
    # nehmen (frischester Formular-Link).
    groups: dict[tuple[str, str | None], list] = {}
    for t in tokens:
        key = ((t.kunde_email or "").lower(), t.kunde_telefon or None)
        groups.setdefault(key, []).append(t)
    for key in groups:
        groups[key].sort(key=lambda t: t.created_at, reverse=True)

    try:
        adapter = await get_calendar_adapter(tenant.id)
    except Exception as e:
        logger.warning(
            f"Reminder-Cron tenant={tenant.slug}: kein Calendar-Adapter "
            f"({e}) — skip"
        )
        return len(tokens), 0

    now_utc = dt.datetime.now(dt.timezone.utc)
    win_min = now_utc + dt.timedelta(hours=REMINDER_WINDOW_MIN_HOURS)
    win_max = now_utc + dt.timedelta(hours=REMINDER_WINDOW_MAX_HOURS)

    sent_count = 0
    for (email_lower, telefon), grp_tokens in groups.items():
        try:
            events = await adapter.find_events(
                time_min=win_min, time_max=win_max,
                kunde_telefon_normalized=telefon,
                kunde_email=email_lower or None,
            )
        except Exception as e:
            logger.warning(
                f"Reminder-Cron tenant={tenant.slug} kunde={email_lower}: "
                f"calendar-find-events failed ({e}) — skip group"
            )
            continue
        if not events:
            continue

        primary = grp_tokens[0]  # neuester Token der Gruppe
        ev = min(events, key=lambda e: e.get("start_dt") or win_max)
        start = ev.get("start_dt") or win_min

        company_name = (
            getattr(tenant, "company_name", None)
            or getattr(tenant, "slug", "")
            or "dein Handwerker"
        )
        form_url = build_anfrage_url(primary.token)
        body_html = _reminder_mail_html(
            kunde_anrede=extract_first_name(primary.kunde_name) or "",
            company_name=company_name,
            form_url=form_url,
            termin_label=_termin_label(start),
        )
        try:
            ok = await send_mail_as_user(
                tenant_id=tenant.id,
                to_email=primary.kunde_email,
                subject=f"Erinnerung: Dein Anfrage-Formular fuer {company_name}",
                body_html=body_html,
            )
            if not ok:
                logger.warning(
                    f"Reminder-Cron tenant={tenant.slug} kunde={email_lower}: "
                    f"Mail-Send hat False zurueckgegeben — kein Mark"
                )
                continue
        except Exception as e:
            logger.exception(
                f"Reminder-Cron tenant={tenant.slug} kunde={email_lower}: "
                f"Mail-Send fehlgeschlagen: {e}"
            )
            continue

        # Alle Tokens der Gruppe markieren — sonst flutet der naechste
        # Tick mit denselben Reminder-Kandidaten zurueck.
        for t in grp_tokens:
            await mark_reminder_sent(t.id)
        sent_count += 1
        logger.info(
            f"Reminder-Cron tenant={tenant.slug}: Erinnerung an "
            f"{primary.kunde_email} gesendet (Termin {_termin_label(start)}, "
            f"{len(grp_tokens)} Token(s) markiert)"
        )

    return len(tokens), sent_count


async def _tick() -> None:
    async with AsyncSessionLocal() as s:
        tenants = (await s.execute(select(Tenant))).scalars().all()
        for t in tenants:
            s.expunge(t)
    total_checked = 0
    total_sent = 0
    for t in tenants:
        try:
            checked, sent = await _process_tenant(t)
            total_checked += checked
            total_sent += sent
        except Exception as e:
            logger.exception(
                f"Reminder-Cron tenant={t.slug} unerwarteter Fehler: {e}"
            )
    if total_checked or total_sent:
        logger.info(
            f"Reminder-Cron Lauf fertig: {total_checked} offene Tokens "
            f"geprueft, {total_sent} Mails verschickt"
        )


async def cron_loop() -> None:
    """Backgroundtask: tick stuendlich."""
    logger.info(
        f"Anfrage-Reminder-Cron gestartet "
        f"(Intervall {TICK_INTERVAL_SECONDS}s, Fenster "
        f"{REMINDER_WINDOW_MIN_HOURS}-{REMINDER_WINDOW_MAX_HOURS}h vor Termin)"
    )
    from core.integrations.cron_health import record_heartbeat
    while True:
        try:
            await _tick()
        except Exception as e:
            logger.exception(f"Reminder-Cron Tick fehlgeschlagen: {e}")
        record_heartbeat("anfrage_reminder")
        await asyncio.sleep(TICK_INTERVAL_SECONDS)
