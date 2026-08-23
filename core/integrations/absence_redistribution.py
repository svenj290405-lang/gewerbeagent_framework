"""Absence-Redistribution — automatische Termin-Umverteilung bei
Mitarbeiter-Krankmeldung.

Trigger:
1. Sofort bei der Krankmeldung in der App (fire-and-forget via
   schedule_immediate_redistribution).
2. Taeglich morgens 06:00 Europe/Berlin (cron_loop) — fuer Faelle wo
   eine Krankmeldung mehrere Tage abdeckt: jeden Tag erneut die heutigen
   Events des Erkrankten umverteilen.

Algorithmus pro Event:
1. Skill aus Event-Subject/Description extrahieren
   (extract_skills_from_text).
2. choose_employee(target_datetime=event.start, exclude=[sick_id]).
3. Bei reason="no-coverage": Eskalation an den Default-Employee.
4. Bei Erfolg: Event im sick-Kalender loeschen + im substitute-Kalender
   anlegen (provider-agnostisch).
5. Optional: Kunden-Reschedule-Mail wenn wir eine Mail haben (z.B. via
   Kundengespraech-Lookup auf gleichem Kunden-Namen heuristisch — V1
   nur wenn explizit verlinkt).

Idempotenz: pro (tenant_id, sick_emp_id, day) ein In-Memory-_inflight-
Set verhindert Doppellauf zwischen sofort-Trigger und Cron. Plus pro
Event ein „bereits verschoben?"-Check via abgesucht-In-Memory-Set
das den Event-Inhalt (id+kalender) merkt.

Failsafe: kein einziger Termin-Fail darf den Rest stoppen — try/except
ueber alles. Cron-Heartbeat bleibt aktiv auch wenn keine Events.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.models import Tenant
from core.models.employee import Employee, get_default_employee
from core.models.employee_absence import (
    ABSENCE_KRANK,
    EmployeeAbsence,
    get_active_absences,
)
from core.routing.employee_router import (
    choose_employee, extract_skills_from_text,
)

logger = logging.getLogger(__name__)


# Cron-Konfiguration: pro Loop-Cycle 60s schlafen, aber nur einmal
# pro Tag um 06:00 Europe/Berlin echte Arbeit machen.
LOOP_TICK_SECONDS = 60
TARGET_HOUR = 6  # Europe/Berlin

# In-Memory-Anti-Doppellauf: (tenant_id, sick_emp_id, date)
_inflight: set[tuple[UUID, UUID, dt.date]] = set()
# Bereits umverteilte Events in dieser Cron-Iteration:
# Key (tenant_id, calendar_event_id) — verhindert Doppel-Move im
# gleichen Lauf wenn ein Event mehrfach in der Liste auftaucht.
_processed_events: set[tuple[UUID, str]] = set()


# ---------------------------------------------------------------------
# Datenklassen fuer Reports
# ---------------------------------------------------------------------


@dataclass
class EventRedistributionResult:
    event_id: str
    event_subject: str
    event_start: dt.datetime
    sick_emp_slug: str
    new_emp_slug: str | None
    reason: str  # 'moved' | 'no-coverage' | 'error' | 'skipped'
    error: str | None = None


@dataclass
class RedistributionReport:
    sick_emp_slug: str
    sick_emp_name: str
    date_range: tuple[dt.date, dt.date]
    reassigned: list[EventRedistributionResult] = field(default_factory=list)
    no_coverage: list[EventRedistributionResult] = field(default_factory=list)
    errors: list[EventRedistributionResult] = field(default_factory=list)


# ---------------------------------------------------------------------
# Provider-agnostische Calendar-Operationen
# ---------------------------------------------------------------------


async def _list_events_for_employee_day(
    employee: Employee, day: dt.date,
) -> list[dict]:
    """Liefert die Events des Tages aus dem Kalender des Mitarbeiters.
    Provider via employee.calendar_provider."""
    provider = (employee.calendar_provider or "").lower()
    if provider == "google":
        from core.integrations.google_calendar import list_events_for_day
        return await list_events_for_day(
            employee.tenant_id, day,
            employee_id=employee.id,
            calendar_id=employee.calendar_id or "primary",
        )
    elif provider == "microsoft":
        from core.integrations.microsoft_calendar import list_events_for_day
        return await list_events_for_day(
            employee.tenant_id, day, employee_id=employee.id,
        )
    return []


async def _event_volltext(employee: Employee, event_id: str) -> dict:
    """Beschreibung + Metadaten eines Events, ungekuerzt.

    Die Tagesliste liefert nur ``body_preview`` (Google 300 Zeichen,
    Microsoft ~255). Genau am Ende der Beschreibung stehen aber der
    Drive-Link und die GA-Ref, und die extendedProperties (Kunden-Mail
    und -Telefon) fehlen in der Liste ganz. Ohne diesen Extra-Aufruf
    entsteht beim Umziehen eine verstuemmelte Kopie, in der die
    Storno-Suche den Kunden nicht mehr findet.
    """
    provider = (employee.calendar_provider or "").lower()
    try:
        if provider == "google":
            from core.integrations.google_calendar import get_event_details
            return await get_event_details(
                employee.tenant_id, event_id,
                employee_id=employee.id,
                calendar_id=employee.calendar_id or "primary",
            )
        if provider == "microsoft":
            from core.integrations.microsoft_calendar import get_event_details
            return await get_event_details(
                employee.tenant_id, event_id, employee_id=employee.id,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"_event_volltext({event_id}) fehlgeschlagen: {exc}")
    return {"description": "", "props": {}}


async def _create_event_for_employee(
    employee: Employee, *,
    summary: str, description: str, location: str,
    start: dt.datetime, end: dt.datetime,
    props: dict | None = None,
) -> dict:
    """Anlegen im Kalender des Mitarbeiters.

    ``props`` sind die strukturierten Metadaten des Original-Events
    (kunde_telefon, kunde_email, ga_ref …). Sie MUESSEN mitwandern:
    ``find_events`` sucht darueber exakt, und ohne sie waere ein
    umverteilter Termin per Kunden-Mail oder Telefonnummer nicht mehr
    auffindbar — der Kunde sagt ab und Q findet nichts.
    """
    props = props or {}
    telefon = props.get("kunde_telefon") or None
    email = props.get("kunde_email") or None
    ga_ref = props.get("ga_ref") or None
    # Alles ausser den drei bekannten Keys unveraendert durchreichen
    # (z.B. ga_mirror), damit spaetere Marker nicht still verlorengehen.
    zusatz = {
        k: v for k, v in props.items()
        if k not in ("kunde_telefon", "kunde_email", "ga_ref") and v
    }

    provider = (employee.calendar_provider or "").lower()
    if provider == "google":
        # Ueber den Plugin-Adapter statt core.integrations.google_calendar
        # .create_event: nur der Adapter schreibt extendedProperties.
        from plugins.kalender.adapters import GoogleCalendarAdapter
        adapter = GoogleCalendarAdapter(
            employee.tenant_id,
            calendar_id=employee.calendar_id or "primary",
            employee_id=employee.id,
        )
        return await adapter.create_event(
            summary=summary, description=description, location=location,
            start=start, end=end, timezone="Europe/Berlin",
            kunde_telefon_normalized=telefon, kunde_email=email,
            idempotency_key=ga_ref, zusatz_props=zusatz or None,
        )
    elif provider == "microsoft":
        from core.integrations.microsoft_calendar import create_event
        return await create_event(
            employee.tenant_id,
            summary=summary, description=description, location=location,
            start=start, end=end,
            employee_id=employee.id,
            kunde_telefon_normalized=telefon, kunde_email=email,
            idempotency_key=ga_ref, zusatz_props=zusatz or None,
        )
    raise RuntimeError(
        f"Employee {employee.slug} hat keinen calendar_provider — "
        f"kann Event nicht anlegen"
    )


async def _delete_event_from_employee(
    employee: Employee, event_id: str,
) -> bool:
    """Loeschen aus dem Kalender des Mitarbeiters."""
    provider = (employee.calendar_provider or "").lower()
    if provider == "google":
        from core.integrations.google_calendar import delete_event
        return await delete_event(
            employee.tenant_id, event_id,
            employee_id=employee.id,
            calendar_id=employee.calendar_id or "primary",
        )
    elif provider == "microsoft":
        from core.integrations.microsoft_calendar import delete_event
        return await delete_event(
            employee.tenant_id, event_id, employee_id=employee.id,
        )
    return False


# ---------------------------------------------------------------------
# Kernfunktion: Umverteilung eines Mitarbeiters fuer einen Zeitraum
# ---------------------------------------------------------------------


async def _move_event(
    tenant: Tenant, sick_emp: Employee, new_emp: Employee, event: dict,
) -> dict:
    """Verschiebt EIN Event von sick_emp's Kalender in new_emp's Kalender.

    Reihenfolge: ERST create im neuen, DANN delete im alten.
    Bei create-Fehler: nichts mehr veraendert.
    Bei delete-Fehler nach erfolgreichem create: Event ist doppelt,
    aber das ist besser als Event-verloren — Inhaber sieht's im Briefing.

    Returns: {"new_event_id": ..., "html_link": ...}
    """
    # Volltext + Metadaten NACHLADEN. event["body_preview"] aus der
    # Tagesliste ist gekuerzt; eine Kopie daraus verliert Drive-Link und
    # Kunden-Metadaten (siehe _event_volltext).
    voll = await _event_volltext(sick_emp, event.get("event_id") or "")
    beschreibung = voll.get("description") or event.get("body_preview") or ""
    props = dict(voll.get("props") or {})
    # Vermerk, damit im Kalender steht, warum hier jemand anderes drinsteht.
    hinweis = (
        f"\n\nUebernommen von {sick_emp.name} "
        f"(abwesend) — automatisch umverteilt."
    )
    if "umverteilt" not in beschreibung.lower():
        beschreibung = f"{beschreibung}{hinweis}"
    props["ga_umverteilt_von"] = sick_emp.slug

    new_event = await _create_event_for_employee(
        new_emp,
        summary=event.get("subject") or "(Termin)",
        description=beschreibung,
        location=event.get("location") or "",
        start=event["start_dt"],
        end=event["end_dt"],
        props=props,
    )
    delete_ok = await _delete_event_from_employee(
        sick_emp, event.get("event_id") or "",
    )
    if not delete_ok:
        logger.warning(
            f"_move_event: Quell-Event nicht geloescht "
            f"(sick={sick_emp.slug}, event={event.get('event_id')}) — "
            f"manuell pruefen!"
        )
    return new_event


async def _handle_one_event(
    tenant: Tenant, sick_emp: Employee, event: dict, day: dt.date,
) -> EventRedistributionResult:
    """Verarbeitet ein einzelnes Event: Kandidat finden + verschieben."""
    event_id = event.get("event_id") or ""
    subject = event.get("subject") or ""
    location = event.get("location") or ""
    body = event.get("body_preview") or ""
    start_dt = event["start_dt"]

    # Idempotenz: schon in diesem Cron-Lauf bearbeitet?
    key = (tenant.id, event_id)
    if key in _processed_events:
        return EventRedistributionResult(
            event_id=event_id, event_subject=subject,
            event_start=start_dt, sick_emp_slug=sick_emp.slug,
            new_emp_slug=None, reason="skipped",
            error="bereits in diesem Lauf bearbeitet",
        )
    _processed_events.add(key)

    # Skill-Match + Adresse-Hinweis aus Event-Daten
    anliegen_text = f"{subject} {body}"
    decision = await choose_employee(
        tenant.id,
        anliegen_text=anliegen_text,
        kunde_adresse=location or None,
        target_datetime=start_dt,
        exclude_employee_ids=[sick_emp.id],
    )

    if decision is None or decision.reason == "no-coverage":
        return EventRedistributionResult(
            event_id=event_id, event_subject=subject,
            event_start=start_dt, sick_emp_slug=sick_emp.slug,
            new_emp_slug=None, reason="no-coverage",
        )

    # Kandidat hat Kalender-Provider?
    async with AsyncSessionLocal() as s:
        new_emp = (await s.execute(
            select(Employee).where(Employee.id == decision.employee_id)
        )).scalar_one_or_none()
        if new_emp is not None:
            s.expunge(new_emp)
    if new_emp is None or not new_emp.calendar_provider:
        return EventRedistributionResult(
            event_id=event_id, event_subject=subject,
            event_start=start_dt, sick_emp_slug=sick_emp.slug,
            new_emp_slug=decision.employee_slug, reason="error",
            error="Kandidat hat keinen verbundenen Kalender",
        )

    # Sanity-Check: sick_emp hat selber einen Kalender? (Sonst koennen
    # wir das Quell-Event nicht loeschen — Pipeline trotzdem laufen
    # lassen, aber log.)
    if not sick_emp.calendar_provider:
        return EventRedistributionResult(
            event_id=event_id, event_subject=subject,
            event_start=start_dt, sick_emp_slug=sick_emp.slug,
            new_emp_slug=None, reason="error",
            error="Erkrankter hat keinen verbundenen Kalender",
        )

    try:
        new_event = await _move_event(tenant, sick_emp, new_emp, event)
        # Beide Beteiligten direkt benachrichtigen — Inhaber bekommt
        # zusaetzlich die Sammel-Zusammenfassung am Ende ueber
        # _send_report_to_inhaber. Pushes sind silent-fail: Versand-
        # Fehler stoppen die Umverteilung nicht.
        await _notify_move(tenant, sick_emp, new_emp, event, start_dt)
        return EventRedistributionResult(
            event_id=event_id, event_subject=subject,
            event_start=start_dt, sick_emp_slug=sick_emp.slug,
            new_emp_slug=new_emp.slug, reason="moved",
        )
    except Exception as e:  # noqa: BLE001
        logger.exception(
            f"_handle_one_event: move failed event={event_id}: {e}"
        )
        return EventRedistributionResult(
            event_id=event_id, event_subject=subject,
            event_start=start_dt, sick_emp_slug=sick_emp.slug,
            new_emp_slug=new_emp.slug if new_emp else None, reason="error",
            error=str(e)[:200],
        )


async def _notify_move(
    tenant: Tenant, sick_emp: Employee, new_emp: Employee,
    event: dict, start_dt: dt.datetime,
) -> None:
    """Benachrichtigt sick_emp und new_emp ueber die Umverteilung.

    Silent-fail: Versandfehler werden geloggt aber nicht weitergereicht
    (Umverteilung selbst soll nicht an Push-Problemen scheitern).
    """
    from core.integrations.notify import notify_employee
    when = start_dt.strftime("%a %d.%m. %H:%M")
    try:
        await notify_employee(
            tenant.id, sick_emp.id,
            title="Termin umgehängt",
            body=f"{when} — ein Kollege übernimmt. Details in der App.",
            url="/app#termine", tag="umverteilung",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"_notify_move sick push failed: {e}")
    try:
        await notify_employee(
            tenant.id, new_emp.id,
            title="Du übernimmst einen Termin",
            body=f"{when} — Details in der App.",
            url="/app#termine", tag="umverteilung",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"_notify_move new push failed: {e}")


async def redistribute_for_employee(
    tenant_id: UUID,
    sick_employee_id: UUID,
    date_range: tuple[dt.date, dt.date],
) -> RedistributionReport:
    """Verteilt die Termine eines Erkrankten ueber den date_range neu.

    Wird sowohl von /krank (sofort) als auch von cron_loop (taeglich)
    aufgerufen. Idempotent ueber _inflight + _processed_events.
    """
    start, end = date_range
    if end < start:
        start, end = end, start

    async with AsyncSessionLocal() as s:
        tenant = (await s.execute(
            select(Tenant).where(Tenant.id == tenant_id)
        )).scalar_one_or_none()
        sick = (await s.execute(
            select(Employee).where(Employee.id == sick_employee_id)
        )).scalar_one_or_none()
        if tenant is None or sick is None:
            return RedistributionReport(
                sick_emp_slug="?", sick_emp_name="?",
                date_range=(start, end),
            )
        s.expunge(tenant)
        s.expunge(sick)

    report = RedistributionReport(
        sick_emp_slug=sick.slug, sick_emp_name=sick.name,
        date_range=(start, end),
    )

    # Iteriere pro Tag im Range
    cur = start
    while cur <= end:
        flight_key = (tenant.id, sick.id, cur)
        if flight_key in _inflight:
            logger.info(
                f"redistribute: skip — bereits in flight {flight_key}"
            )
            cur += dt.timedelta(days=1)
            continue
        _inflight.add(flight_key)
        try:
            events = await _list_events_for_employee_day(sick, cur)
            for event in events:
                try:
                    res = await _handle_one_event(tenant, sick, event, cur)
                    if res.reason == "moved":
                        report.reassigned.append(res)
                    elif res.reason == "no-coverage":
                        report.no_coverage.append(res)
                    elif res.reason == "error":
                        report.errors.append(res)
                    # "skipped" wird nicht reported
                except Exception as e:  # noqa: BLE001
                    logger.exception(
                        f"redistribute: handle_one_event crashed: {e}"
                    )
        finally:
            _inflight.discard(flight_key)
        cur += dt.timedelta(days=1)

    return report


# ---------------------------------------------------------------------
# Eskalation an Inhaber wenn kein Kollege verfuegbar
# ---------------------------------------------------------------------


async def _send_report_to_inhaber(tenant: Tenant, report: RedistributionReport):
    """Schickt die Zusammenfassung an den Default-Employee (Web-Push).

    Zaehler und Details bleiben im Server — der Push verweist nur auf die
    App (Team-/Termin-Screen).
    """
    try:
        from core.integrations.notify import notify_tenant
        default = await get_default_employee(tenant.id)
        if default is None:
            logger.info(
                f"_send_report_to_inhaber: kein Default-Employee fuer "
                f"tenant={tenant.slug} — skip"
            )
            return
        # Zahlen gehoeren in den Push: "Zusammenfassung in der App" war
        # ein leeres Versprechen — die App zeigt keinen Umverteilungs-
        # Report. Zahlen sind keine Kunden-PII und beantworten die einzige
        # Frage, die der Inhaber sofort hat: muss ich eingreifen?
        teile = []
        if report.reassigned:
            teile.append(f"{len(report.reassigned)} Termin(e) uebernommen")
        if report.no_coverage:
            teile.append(
                f"{len(report.no_coverage)} OHNE Ersatz — bitte selbst klaeren"
            )
        if report.errors:
            teile.append(f"{len(report.errors)} Fehler")
        body = "; ".join(teile) or "Nichts zu verteilen."
        # Bei fehlender Abdeckung ist es kein Report mehr, sondern eine
        # Aufgabe — der Titel soll das unterscheiden.
        titel = (
            f"Krankmeldung {report.sick_emp_name}: Termine offen"
            if report.no_coverage
            else f"Termine von {report.sick_emp_name} umverteilt"
        )
        await notify_tenant(
            tenant.id,
            title=titel,
            body=body,
            url="/app#termine", tag="umverteilung-report",
            employee_id=default.id,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"_send_report_to_inhaber: Versand failed: {e}"
        )


# ---------------------------------------------------------------------
# Public-API: Sofort-Trigger + Cron-Loop
# ---------------------------------------------------------------------


def schedule_immediate_redistribution(
    tenant_id: UUID,
    sick_employee_id: UUID,
    date_range: tuple[dt.date, dt.date],
) -> asyncio.Task:
    """Fire-and-forget: startet die Umverteilung im Hintergrund + schickt
    dem Inhaber am Ende den Report als Push.

    Wird aus der Krankmeldung in der App aufgerufen — der User soll nicht
    30s auf Calendar-APIs warten muessen.
    """
    async def _run():
        try:
            report = await redistribute_for_employee(
                tenant_id, sick_employee_id, date_range,
            )
            async with AsyncSessionLocal() as s:
                tenant = (await s.execute(
                    select(Tenant).where(Tenant.id == tenant_id)
                )).scalar_one_or_none()
                if tenant is not None:
                    s.expunge(tenant)
                    await _send_report_to_inhaber(tenant, report)
        except Exception as e:  # noqa: BLE001
            logger.exception(
                f"schedule_immediate_redistribution crashed: {e}"
            )
    return asyncio.create_task(_run())


async def cron_loop() -> None:
    """Taeglich 06:00 Europe/Berlin: alle aktiven Krank-Absences finden
    und HEUTE umverteilen. Tickt im Minutentakt fuer Heartbeat.
    """
    logger.info("Absence-Redistribution-Cron gestartet (taegl. 06:00 Europe/Berlin)")
    await asyncio.sleep(60)  # App-Start abwarten

    from core.integrations.cron_health import record_heartbeat
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Europe/Berlin")
    except Exception:
        tz = dt.timezone.utc

    last_run_day: dt.date | None = None

    while True:
        try:
            now = dt.datetime.now(tz)
            record_heartbeat("absence_redistribution")

            # Triggern wenn Stunde == TARGET_HOUR und heute noch nicht gelaufen
            if now.hour == TARGET_HOUR and last_run_day != now.date():
                today = now.date()
                logger.info(
                    f"Absence-Redistribution Cron-Lauf fuer {today}"
                )
                await _run_cron_for_today(today)
                last_run_day = today

            await asyncio.sleep(LOOP_TICK_SECONDS)
        except asyncio.CancelledError:
            logger.info("Absence-Redistribution-Cron gestoppt")
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception(f"Absence-Cron-Loop Fehler: {e}")
            await asyncio.sleep(60)


async def _run_cron_for_today(today: dt.date) -> None:
    """Iteriert alle Tenants → alle aktiven Krank-Absences → umverteilen."""
    # _processed_events dedupt nur INNERHALB eines Laufs (ein Event kann bei
    # mehreren kranken Mitarbeitern auftauchen). Zu Lauf-Beginn leeren: sonst
    # waechst das Set unbegrenzt (Memory-Leak) UND ein in einem frueheren Lauf
    # NICHT verschobenes Event (z.B. damals kein Ziel-MA frei) koennte nie
    # wieder umverteilt werden.
    _processed_events.clear()
    # Wir laden alle Tenants und je Tenant alle aktiven Krank-Absences heute.
    async with AsyncSessionLocal() as s:
        tenants = (await s.execute(select(Tenant))).scalars().all()
        for t in tenants:
            s.expunge(t)

    for tenant in tenants:
        try:
            active = await get_active_absences(tenant.id, today)
            for emp, absence in active:
                if absence.absence_type != ABSENCE_KRANK:
                    # Urlaub wird vorausgeplant — Bestands-Termine NICHT
                    # umverteilen (Inhaber-Wille). Nur fuer 'krank'.
                    continue
                logger.info(
                    f"Cron: Umverteile {emp.slug} fuer {today} "
                    f"(tenant={tenant.slug})"
                )
                report = await redistribute_for_employee(
                    tenant.id, emp.id, (today, today),
                )
                # Nur reporten wenn was passiert ist
                if (
                    report.reassigned or report.no_coverage or report.errors
                ):
                    await _send_report_to_inhaber(tenant, report)
        except Exception as e:  # noqa: BLE001
            logger.exception(
                f"_run_cron_for_today: tenant {tenant.slug} crashed: {e}"
            )
