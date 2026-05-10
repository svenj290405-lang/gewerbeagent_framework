"""Calendar-Adapter — abstrahiert Google Calendar v3 vs Microsoft Graph.

Wird von plugins/kalender/handler.py genutzt damit ein Mitarbeiter
beim Onboarding waehlen kann ob sein Kalender Google oder Microsoft
ist. Beide Adapter implementieren das selbe schmale Interface, sodass
die High-Level-Logik (FreeBusy-Check, Slot-Suche, Termin-Buchung,
Smart-Filter) provider-unabhaengig laeuft.

Nicht im Adapter (bleibt im Plugin):
- Arbeitszeit-/Werktag-Check
- Slot-Auswahl-Heuristik
- Skill-Routing-Anbindung

Im Adapter:
- is_slot_busy: ein konkretes Zeitfenster — frei oder belegt?
- get_busy_periods: alle busy-Intervalle in einem Zeitraum
- list_events_for_day: alle Events eines Tages mit location (fuer
  Smart-Filter, um Vor-/Nach-Termine zu finden)
- create_event: neuen Termin anlegen
- delete_event: Termin loeschen
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)


class CalendarAdapter(ABC):
    """Provider-unabhaengiges Calendar-Interface."""

    provider_name: str  # 'google' | 'microsoft'

    @abstractmethod
    async def is_slot_busy(self, start: dt.datetime, end: dt.datetime) -> bool:
        """True wenn im Zeitfenster [start, end] mindestens ein Event liegt."""

    @abstractmethod
    async def get_busy_periods(
        self, time_min: dt.datetime, time_max: dt.datetime,
    ) -> list[dict[str, Any]]:
        """Liste von {"start": iso-str, "end": iso-str} im Zeitraum."""

    @abstractmethod
    async def list_events_for_day(
        self, target_date: dt.date,
    ) -> list[dict[str, Any]]:
        """Alle Events des Tages: {"start_dt", "end_dt", "location"}."""

    @abstractmethod
    async def create_event(
        self, *,
        summary: str,
        description: str,
        location: str,
        start: dt.datetime,
        end: dt.datetime,
        timezone: str,
    ) -> dict[str, Any]:
        """Anlegen. Returns: {"id": ..., "html_link": ...}."""

    @abstractmethod
    async def delete_event(self, event_id: str) -> bool:
        """Loeschen. True bei Erfolg oder schon-weg."""


# =====================================================================
# GOOGLE Calendar Adapter
# =====================================================================

class GoogleCalendarAdapter(CalendarAdapter):
    """Wrappt die bestehende google_auth + googleapiclient-Logik."""
    provider_name = "google"

    def __init__(self, tenant_id: uuid.UUID, calendar_id: str, employee_id: uuid.UUID | None = None):
        self.tenant_id = tenant_id
        self.calendar_id = calendar_id
        self.employee_id = employee_id
        self._service = None

    async def _get_service(self):
        if self._service is None:
            from plugins.kalender.google_auth import get_calendar_service
            self._service = await get_calendar_service(
                self.tenant_id, employee_id=self.employee_id,
            )
        return self._service

    @staticmethod
    def _tz_offset() -> str:
        # TODO: aus tenant-Config; pragmatisch jetzt "+02:00" wie bisher
        return "+02:00"

    async def is_slot_busy(self, start, end) -> bool:
        service = await self._get_service()
        tz = self._tz_offset()
        result = service.events().list(
            calendarId=self.calendar_id,
            timeMin=start.isoformat() + tz,
            timeMax=end.isoformat() + tz,
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        return bool(result.get("items"))

    async def get_busy_periods(self, time_min, time_max):
        service = await self._get_service()
        tz = self._tz_offset()
        body = {
            "timeMin": time_min.isoformat() + tz,
            "timeMax": time_max.isoformat() + tz,
            "items": [{"id": self.calendar_id}],
        }
        result = service.freebusy().query(body=body).execute()
        return result.get("calendars", {}).get(self.calendar_id, {}).get("busy", [])

    async def list_events_for_day(self, target_date):
        from dateutil import parser as _p  # type: ignore
        service = await self._get_service()
        day_start = dt.datetime.combine(target_date, dt.time(0, 0)).isoformat() + self._tz_offset()
        day_end = dt.datetime.combine(target_date, dt.time(23, 59)).isoformat() + self._tz_offset()
        try:
            resp = service.events().list(
                calendarId=self.calendar_id,
                timeMin=day_start, timeMax=day_end,
                singleEvents=True, orderBy="startTime",
            ).execute()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Google list_events_for_day({target_date}) failed: {exc}")
            return []

        events = []
        for ev in resp.get("items", []):
            s = ev.get("start", {})
            e = ev.get("end", {})
            s_iso = s.get("dateTime") or s.get("date")
            e_iso = e.get("dateTime") or e.get("date")
            if not s_iso or not e_iso:
                continue
            events.append({
                "start_dt": _p.isoparse(s_iso).replace(tzinfo=None),
                "end_dt": _p.isoparse(e_iso).replace(tzinfo=None),
                "location": (ev.get("location") or "").strip(),
            })
        return events

    async def create_event(self, *, summary, description, location, start, end, timezone):
        service = await self._get_service()
        body = {
            "summary": summary,
            "description": description,
            "location": location,
            "start": {"dateTime": start.isoformat(), "timeZone": timezone},
            "end": {"dateTime": end.isoformat(), "timeZone": timezone},
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "popup", "minutes": 60},
                    {"method": "popup", "minutes": 1440},
                ],
            },
        }
        result = service.events().insert(
            calendarId=self.calendar_id, body=body,
        ).execute()
        return {
            "id": result.get("id"),
            "html_link": result.get("htmlLink", ""),
        }

    async def delete_event(self, event_id):
        service = await self._get_service()
        try:
            service.events().delete(
                calendarId=self.calendar_id, eventId=event_id,
            ).execute()
            return True
        except Exception as exc:  # noqa: BLE001
            # Google wirft HttpError 404 wenn schon weg → idempotent ok
            if "404" in str(exc) or "Not Found" in str(exc):
                return True
            logger.warning(f"Google delete_event({event_id}) failed: {exc}")
            return False


# =====================================================================
# MICROSOFT Outlook Adapter
# =====================================================================

class MicrosoftCalendarAdapter(CalendarAdapter):
    """Nutzt core/integrations/microsoft_calendar.py (Graph API)."""
    provider_name = "microsoft"

    def __init__(
        self, tenant_id: uuid.UUID,
        calendar_id: str | None = None,
        employee_id: uuid.UUID | None = None,
    ):
        self.tenant_id = tenant_id
        self.employee_id = employee_id
        # calendar_id wird aktuell nicht genutzt — Microsoft schreibt
        # immer in /me/events (primaerer Kalender). Feld vorgesehen
        # damit spaeter sekundaere Outlook-Kalender unterstuetzt werden
        # koennen (/me/calendars/{id}/events).
        self.calendar_id = calendar_id

    async def is_slot_busy(self, start, end):
        from core.integrations.microsoft_calendar import get_free_busy
        busy = await get_free_busy(
            self.tenant_id, start=start, end=end, employee_id=self.employee_id,
        )
        return bool(busy)

    async def get_busy_periods(self, time_min, time_max):
        from core.integrations.microsoft_calendar import get_free_busy
        busy = await get_free_busy(
            self.tenant_id, start=time_min, end=time_max,
            employee_id=self.employee_id,
        )
        # Format-Angleichung an Google: {"start": iso, "end": iso}
        return [
            {
                "start": b["start_dt"].isoformat(),
                "end": b["end_dt"].isoformat(),
            }
            for b in busy
        ]

    async def list_events_for_day(self, target_date):
        from core.integrations.microsoft_calendar import list_events_for_day
        return await list_events_for_day(
            self.tenant_id, target_date, employee_id=self.employee_id,
        )

    async def create_event(self, *, summary, description, location, start, end, timezone):
        # Microsoft nutzt fixe Tenant-Default-TZ via Helper; timezone-
        # Param wird in zukuenftiger Version genutzt.
        _ = timezone
        from core.integrations.microsoft_calendar import create_event
        return await create_event(
            self.tenant_id,
            summary=summary, description=description,
            location=location, start=start, end=end,
            employee_id=self.employee_id,
        )

    async def delete_event(self, event_id):
        from core.integrations.microsoft_calendar import delete_event
        return await delete_event(
            self.tenant_id, event_id, employee_id=self.employee_id,
        )


# =====================================================================
# FACTORY
# =====================================================================

async def get_calendar_adapter(
    tenant_id: uuid.UUID,
    employee_id: uuid.UUID | None = None,
    fallback_calendar_id: str = "primary",
) -> CalendarAdapter:
    """Liefert den passenden Adapter fuer einen Mitarbeiter.

    employee_id None → Default-Employee des Tenants. Wenn der
    Employee keinen calendar_provider hat, wird Google angenommen
    (Backward-Compat). calendar_id-Hierarchie:
    1. employee.calendar_id (wenn gesetzt)
    2. fallback_calendar_id (Plugin-Config: 'primary' oder spezifisch)
    """
    from core.models.employee import (
        Employee,
        get_default_employee,
        CALENDAR_PROVIDER_GOOGLE,
        CALENDAR_PROVIDER_MICROSOFT,
    )
    from core.database import AsyncSessionLocal
    from sqlalchemy import select

    if employee_id is None:
        emp = await get_default_employee(tenant_id)
    else:
        async with AsyncSessionLocal() as s:
            emp = (await s.execute(
                select(Employee).where(Employee.id == employee_id)
            )).scalar_one_or_none()

    provider = (emp.calendar_provider if emp else None) or CALENDAR_PROVIDER_GOOGLE
    cal_id = (emp.calendar_id if emp else None) or fallback_calendar_id

    emp_id = emp.id if emp else None

    # Wenn der gewaehlte Employee keinen OAuth-Token fuer den Provider
    # hat, faellt der Lookup automatisch auf den Default-Employee zurueck
    # (siehe core/security/oauth_token_lookup.py — 3-stufiger Fallback).
    # Wir koennen das hier vorab pruefen und ggf. eine Info loggen damit
    # der Owner spaeter im Audit-Log sieht warum ein Termin im falschen
    # Kalender landet.
    try:
        from core.security.oauth_token_lookup import find_oauth_token
        if emp_id is not None:
            token = await find_oauth_token(tenant_id, provider, emp_id)
            if token is not None and token.employee_id != emp_id:
                logger.info(
                    f"Adapter-Fallback: Employee {emp.slug if emp else '?'} hat "
                    f"keinen eigenen {provider}-Token — nutzt Default-Token von "
                    f"employee_id={token.employee_id}"
                )
    except Exception:
        pass

    if provider == CALENDAR_PROVIDER_MICROSOFT:
        return MicrosoftCalendarAdapter(
            tenant_id=tenant_id, calendar_id=cal_id, employee_id=emp_id,
        )
    return GoogleCalendarAdapter(
        tenant_id=tenant_id, calendar_id=cal_id, employee_id=emp_id,
    )
