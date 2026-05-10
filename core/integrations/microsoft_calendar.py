"""Microsoft Graph Calendar Integration.

Analoger Helper zu microsoft_inbox.py — bietet die fuer das kalender-
Plugin noetigen Calendar-Operationen via Graph-API. Wird vom Provider-
Adapter in plugins/kalender benutzt.

Mappt Google-Calendar-Konzepte auf Microsoft-Graph-Endpunkte:

| Google API                                   | Microsoft Graph                            |
|----------------------------------------------|--------------------------------------------|
| service.freebusy().query()                   | POST /me/calendar/getSchedule              |
| service.events().list(timeMin, timeMax)      | GET /me/calendarView?startDateTime=...     |
| service.events().insert(body)                | POST /me/events                            |
| service.events().delete(eventId)             | DELETE /me/events/{id}                     |

Event-Body-Mapping (intern fuer create_event):
- summary → subject
- description → body.content (HTML)
- location (str) → location.displayName
- start/end {dateTime, timeZone} → identisches Format

Token-Auth: nutzt get_microsoft_token(tenant_id) aus microsoft.py,
das automatisch refreshed.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any
from uuid import UUID

import httpx

from core.integrations.microsoft import (
    GRAPH_API_BASE,
    MicrosoftNotConnectedError,
    get_microsoft_token,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEZONE = "Europe/Berlin"
HTTP_TIMEOUT_SECONDS = 20.0


def _iso_no_tz(d: dt.datetime) -> str:
    """ISO-String ohne Timezone-Suffix (Microsoft will das so wenn timeZone-Header gesetzt)."""
    return d.replace(microsecond=0).isoformat(timespec="seconds")


# ---------------------------------------------------------------------
# FREE-BUSY
# ---------------------------------------------------------------------

async def get_free_busy(
    tenant_id: UUID,
    *,
    start: dt.datetime,
    end: dt.datetime,
    schedule_emails: list[str] | None = None,
    employee_id: UUID | None = None,
) -> list[dict[str, Any]]:
    """Liefert busy-Slots im Zeitraum [start, end] fuer den verbundenen Mitarbeiter.

    schedule_emails: optional die Mailadressen die abgefragt werden
    sollen — Default ist der eigene Account ('me').

    Returns: Liste von {"start_dt": datetime (naive), "end_dt": ...}
    """
    token = await get_microsoft_token(tenant_id, employee_id=employee_id)
    if schedule_emails is None:
        # 'me' funktioniert nicht im getSchedule — wir brauchen die
        # E-Mail-Adresse des verbundenen Accounts.
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            me_resp = await client.get(
                f"{GRAPH_API_BASE}/me",
                headers={"Authorization": f"Bearer {token}"},
            )
            me_resp.raise_for_status()
            schedule_emails = [me_resp.json().get("mail") or me_resp.json().get("userPrincipalName")]

    body = {
        "schedules": schedule_emails,
        "startTime": {"dateTime": _iso_no_tz(start), "timeZone": DEFAULT_TIMEZONE},
        "endTime": {"dateTime": _iso_no_tz(end), "timeZone": DEFAULT_TIMEZONE},
        "availabilityViewInterval": 30,
    }
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        resp = await client.post(
            f"{GRAPH_API_BASE}/me/calendar/getSchedule",
            headers={
                "Authorization": f"Bearer {token}",
                "Prefer": f'outlook.timezone="{DEFAULT_TIMEZONE}"',
            },
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()

    busy: list[dict[str, Any]] = []
    for sched in data.get("value", []):
        for item in sched.get("scheduleItems", []):
            try:
                s_iso = item["start"]["dateTime"]
                e_iso = item["end"]["dateTime"]
                busy.append({
                    "start_dt": dt.datetime.fromisoformat(s_iso).replace(tzinfo=None),
                    "end_dt": dt.datetime.fromisoformat(e_iso).replace(tzinfo=None),
                })
            except (KeyError, ValueError) as exc:
                logger.warning(f"Skipping malformed scheduleItem: {exc}")
    return busy


# ---------------------------------------------------------------------
# EVENT-LIST (analog Google events.list)
# ---------------------------------------------------------------------

async def list_events_for_day(
    tenant_id: UUID, target_date: dt.date,
    employee_id: UUID | None = None,
) -> list[dict[str, Any]]:
    """Alle Events des Tages mit Lokation. Wird von Smart-Filter genutzt
    um Vor-/Nach-Termine zu finden.

    Returns: Liste von {"start_dt": datetime, "end_dt": datetime, "location": str}
    """
    token = await get_microsoft_token(tenant_id, employee_id=employee_id)
    day_start = dt.datetime.combine(target_date, dt.time(0, 0))
    day_end = dt.datetime.combine(target_date, dt.time(23, 59))

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        resp = await client.get(
            f"{GRAPH_API_BASE}/me/calendarView",
            headers={
                "Authorization": f"Bearer {token}",
                "Prefer": f'outlook.timezone="{DEFAULT_TIMEZONE}"',
            },
            params={
                "startDateTime": _iso_no_tz(day_start),
                "endDateTime": _iso_no_tz(day_end),
                "$select": "start,end,location",
                "$top": 200,
            },
        )
        resp.raise_for_status()
        data = resp.json()

    events: list[dict[str, Any]] = []
    for ev in data.get("value", []):
        try:
            s_iso = ev["start"]["dateTime"]
            e_iso = ev["end"]["dateTime"]
            loc = (ev.get("location") or {}).get("displayName") or ""
            events.append({
                "start_dt": dt.datetime.fromisoformat(s_iso).replace(tzinfo=None),
                "end_dt": dt.datetime.fromisoformat(e_iso).replace(tzinfo=None),
                "location": loc.strip(),
            })
        except (KeyError, ValueError) as exc:
            logger.warning(f"Skipping malformed event: {exc}")
    return events


# ---------------------------------------------------------------------
# EVENT-CREATE
# ---------------------------------------------------------------------

async def create_event(
    tenant_id: UUID,
    *,
    summary: str,
    description: str,
    location: str,
    start: dt.datetime,
    end: dt.datetime,
    employee_id: UUID | None = None,
) -> dict[str, Any]:
    """Erstellt einen Termin. Returns: {"id": ..., "html_link": ...}"""
    token = await get_microsoft_token(tenant_id, employee_id=employee_id)
    body = {
        "subject": summary,
        "body": {"contentType": "HTML", "content": description.replace("\n", "<br>")},
        "start": {"dateTime": _iso_no_tz(start), "timeZone": DEFAULT_TIMEZONE},
        "end": {"dateTime": _iso_no_tz(end), "timeZone": DEFAULT_TIMEZONE},
        "location": {"displayName": location} if location else None,
    }
    body = {k: v for k, v in body.items() if v is not None}

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        resp = await client.post(
            f"{GRAPH_API_BASE}/me/events",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()
    return {
        "id": data.get("id"),
        "html_link": data.get("webLink") or "",
    }


# ---------------------------------------------------------------------
# EVENT-DELETE
# ---------------------------------------------------------------------

async def delete_event(
    tenant_id: UUID, event_id: str,
    employee_id: UUID | None = None,
) -> bool:
    """Loescht einen Termin. Returns True wenn 204/404 (404 = schon weg)."""
    token = await get_microsoft_token(tenant_id, employee_id=employee_id)
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        resp = await client.delete(
            f"{GRAPH_API_BASE}/me/events/{event_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
    if resp.status_code in (200, 204, 404):
        return True
    logger.warning(
        f"Outlook-Event-Delete unerwarteter Status {resp.status_code}: {resp.text[:200]}"
    )
    return False
