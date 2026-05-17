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
        kunde_telefon_normalized: str | None = None,
        kunde_email: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Anlegen. Returns: {"id": ..., "html_link": ...}.

        Strukturierte Metadaten (kunde_telefon_normalized, kunde_email,
        idempotency_key) werden zusaetzlich zu summary/description als
        provider-spezifische extendedProperties abgelegt. Damit kann
        spaeter `find_events` exakt nach Telefon/Mail suchen statt
        Volltext-Match auf description. Alle drei sind optional —
        Legacy-Caller die nichts mitgeben kriegen ein Event ohne
        Metadaten (Backward-Compat).
        """

    @abstractmethod
    async def delete_event(self, event_id: str) -> bool:
        """Loeschen. True bei Erfolg oder schon-weg."""

    @abstractmethod
    async def find_events(
        self, *,
        time_min: dt.datetime,
        time_max: dt.datetime,
        kunde_telefon_normalized: str | None = None,
        kunde_email: str | None = None,
    ) -> list[dict[str, Any]]:
        """Sucht Events nach Telefon ODER Email im Zeitraum.

        Strategy beider Provider:
        1. PRIMAERE Metadaten-Suche ueber extendedProperties — exakt,
           findet alle Events die nach dem create_event-Refactor
           angelegt wurden.
        2. FALLBACK Volltext-Suche auf description fuer Bestands-Events
           ohne Metadaten. Treffer wird nochmal verifiziert (Telefon-
           Suffix-Match in description / Email-Substring), um zufaellige
           Sub-String-Treffer auszuschliessen.

        Returns: Liste von dicts mit Keys:
          - event_id, start_dt, end_dt (naive Lokal-Zeit)
          - summary, description, location
          - kunde_telefon_match (bool), kunde_email_match (bool)
          - match_source: "metadata" | "fulltext"
        """


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
                # Konsistent zur Microsoft-Variante: subject/event_id/
                # body_preview/web_link durchreichen damit Caller wie
                # /briefing, /termine oder find_events nicht extra eine
                # zweite API-Runde brauchen.
                "subject": (ev.get("summary") or "").strip(),
                "event_id": ev.get("id") or "",
                "body_preview": (ev.get("description") or "")[:300].strip(),
                "web_link": (ev.get("htmlLink") or "").strip(),
            })
        return events

    async def create_event(
        self, *, summary, description, location, start, end, timezone,
        kunde_telefon_normalized=None, kunde_email=None,
        idempotency_key=None,
    ):
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
        # Strukturierte Metadaten: Google's extendedProperties.private
        # ist exakt durchsuchbar via events.list(privateExtendedProperty=
        # "kunde_telefon=..."). Wir setzen nur Keys mit Wert — Google
        # akzeptiert keine None/leeren Strings sauber.
        private_props: dict[str, str] = {}
        if kunde_telefon_normalized:
            private_props["kunde_telefon"] = kunde_telefon_normalized
        if kunde_email:
            private_props["kunde_email"] = kunde_email
        if idempotency_key:
            private_props["ga_ref"] = idempotency_key
        if private_props:
            body["extendedProperties"] = {"private": private_props}

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

    async def find_events(
        self, *, time_min, time_max,
        kunde_telefon_normalized=None, kunde_email=None,
    ):
        from dateutil import parser as _p  # type: ignore
        from plugins.kalender.event_match import (
            verify_fulltext_phone_match, verify_fulltext_email_match,
        )
        service = await self._get_service()
        tz = self._tz_offset()
        t_min = time_min.isoformat() + tz
        t_max = time_max.isoformat() + tz

        results: dict[str, dict[str, Any]] = {}

        def _ingest(items, *, source, phone_match, email_match):
            for ev in items or []:
                s = ev.get("start", {})
                e = ev.get("end", {})
                s_iso = s.get("dateTime") or s.get("date")
                e_iso = e.get("dateTime") or e.get("date")
                if not s_iso or not e_iso:
                    continue
                eid = ev.get("id") or ""
                if not eid:
                    continue
                # Erst-Treffer gewinnt; bei zweitem Treffer flag-Merge
                # damit "via metadata + via fulltext" beides sichtbar
                # ist (match_source bleibt aber der zuerst gefundene).
                if eid in results:
                    if phone_match:
                        results[eid]["kunde_telefon_match"] = True
                    if email_match:
                        results[eid]["kunde_email_match"] = True
                    continue
                results[eid] = {
                    "event_id": eid,
                    "start_dt": _p.isoparse(s_iso).replace(tzinfo=None),
                    "end_dt": _p.isoparse(e_iso).replace(tzinfo=None),
                    "summary": (ev.get("summary") or "").strip(),
                    "description": (ev.get("description") or "").strip(),
                    "location": (ev.get("location") or "").strip(),
                    "kunde_telefon_match": bool(phone_match),
                    "kunde_email_match": bool(email_match),
                    "match_source": source,
                }

        # ----- PRIMAER: extendedProperties.private (exakt) -----
        # Google's privateExtendedProperty-Param: mehrere Werte werden
        # AND-verknuepft. Fuer ODER-Semantik zwei separate Aufrufe.
        if kunde_telefon_normalized:
            try:
                resp = service.events().list(
                    calendarId=self.calendar_id,
                    timeMin=t_min, timeMax=t_max,
                    singleEvents=True, orderBy="startTime",
                    privateExtendedProperty=f"kunde_telefon={kunde_telefon_normalized}",
                ).execute()
                _ingest(resp.get("items", []), source="metadata",
                        phone_match=True, email_match=False)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Google find_events phone-metadata failed: {exc}")
        if kunde_email:
            try:
                resp = service.events().list(
                    calendarId=self.calendar_id,
                    timeMin=t_min, timeMax=t_max,
                    singleEvents=True, orderBy="startTime",
                    privateExtendedProperty=f"kunde_email={kunde_email}",
                ).execute()
                _ingest(resp.get("items", []), source="metadata",
                        phone_match=False, email_match=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Google find_events email-metadata failed: {exc}")

        # ----- FALLBACK: Volltext via q= (mit Verifikation) -----
        # Fuer Legacy-Events ohne extendedProperties. Google's q-Param
        # matcht summary/description/location/attendees. Wir verifizieren
        # nochmal lokal um Random-Treffer auszuschliessen ("0123" matched
        # auch in einer Adresse "Hauptstr. 0123").
        if kunde_telefon_normalized:
            try:
                resp = service.events().list(
                    calendarId=self.calendar_id,
                    timeMin=t_min, timeMax=t_max,
                    singleEvents=True, orderBy="startTime",
                    q=kunde_telefon_normalized[-8:] if len(kunde_telefon_normalized) >= 8 else kunde_telefon_normalized,
                ).execute()
                verified = [
                    ev for ev in resp.get("items", [])
                    if verify_fulltext_phone_match(
                        kunde_telefon_normalized, ev.get("description") or "",
                    )
                ]
                _ingest(verified, source="fulltext",
                        phone_match=True, email_match=False)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Google find_events phone-fulltext failed: {exc}")
        if kunde_email:
            try:
                resp = service.events().list(
                    calendarId=self.calendar_id,
                    timeMin=t_min, timeMax=t_max,
                    singleEvents=True, orderBy="startTime",
                    q=kunde_email,
                ).execute()
                verified = [
                    ev for ev in resp.get("items", [])
                    if verify_fulltext_email_match(
                        kunde_email, ev.get("description") or "",
                    )
                ]
                _ingest(verified, source="fulltext",
                        phone_match=False, email_match=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Google find_events email-fulltext failed: {exc}")

        return list(results.values())


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

    async def create_event(
        self, *, summary, description, location, start, end, timezone,
        kunde_telefon_normalized=None, kunde_email=None,
        idempotency_key=None,
    ):
        # Microsoft nutzt fixe Tenant-Default-TZ via Helper; timezone-
        # Param wird in zukuenftiger Version genutzt.
        _ = timezone
        from core.integrations.microsoft_calendar import create_event
        return await create_event(
            self.tenant_id,
            summary=summary, description=description,
            location=location, start=start, end=end,
            employee_id=self.employee_id,
            kunde_telefon_normalized=kunde_telefon_normalized,
            kunde_email=kunde_email,
            idempotency_key=idempotency_key,
        )

    async def delete_event(self, event_id):
        from core.integrations.microsoft_calendar import delete_event
        return await delete_event(
            self.tenant_id, event_id, employee_id=self.employee_id,
        )

    async def find_events(
        self, *, time_min, time_max,
        kunde_telefon_normalized=None, kunde_email=None,
    ):
        from core.integrations.microsoft_calendar import find_events
        return await find_events(
            self.tenant_id,
            time_min=time_min, time_max=time_max,
            kunde_telefon_normalized=kunde_telefon_normalized,
            kunde_email=kunde_email,
            employee_id=self.employee_id,
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
