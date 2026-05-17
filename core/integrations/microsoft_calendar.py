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

# Property-Set-GUID fuer Gewerbeagent-Metadaten (kunde_telefon,
# kunde_email, ga_ref). Eigene Namespace-UUID damit unsere Properties
# nicht mit Outlook-internen oder anderen Add-Ins kollidieren. Wird
# beim create_event als singleValueExtendedProperties gesetzt und
# beim find_events via $filter abgefragt. NIE aendern — sonst sind
# alle bestehenden Termin-Metadaten unauffindbar.
GA_PROPSET_GUID = "66f5a359-4659-4830-9070-00047ec6ac6e"


def ga_prop_id(name: str) -> str:
    """Baut die singleValueExtendedProperty-Id im Graph-Format."""
    return f"String {{{GA_PROPSET_GUID}}} Name {name}"


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
    """Alle Events des Tages mit Lokation + Subject. Wird genutzt von:
    - Smart-Filter (Vor-/Nach-Termine fuer Fahrtzeit-Rechnung)
    - /briefing (Tages-Uebersicht im Telegram-Bot)

    Returns: Liste von dicts mit Keys:
      - start_dt, end_dt (datetime, naive Lokal-Zeit DEFAULT_TIMEZONE)
      - location (str)
      - subject (str) — Event-Titel (kann leer sein)
      - event_id (str) — Outlook-Event-ID
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
                "$select": "id,subject,start,end,location,bodyPreview",
                "$top": 200,
                "$orderby": "start/dateTime",
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
                "subject": (ev.get("subject") or "").strip(),
                "event_id": ev.get("id") or "",
                "body_preview": (ev.get("bodyPreview") or "").strip(),
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
    kunde_telefon_normalized: str | None = None,
    kunde_email: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Erstellt einen Termin. Returns: {"id": ..., "html_link": ...}.

    Strukturierte Metadaten landen als singleValueExtendedProperties
    am Event — exakt durchsuchbar via /me/events?$filter ueber den
    GA_PROPSET_GUID-Namespace (siehe find_events).
    """
    token = await get_microsoft_token(tenant_id, employee_id=employee_id)
    body: dict[str, Any] = {
        "subject": summary,
        "body": {"contentType": "HTML", "content": description.replace("\n", "<br>")},
        "start": {"dateTime": _iso_no_tz(start), "timeZone": DEFAULT_TIMEZONE},
        "end": {"dateTime": _iso_no_tz(end), "timeZone": DEFAULT_TIMEZONE},
        "location": {"displayName": location} if location else None,
    }
    body = {k: v for k, v in body.items() if v is not None}

    # Strukturierte Metadaten: nur Keys mit Wert. Graph akzeptiert
    # leere Listen problemlos, aber Eintraege mit value=None geben 400.
    ext_props: list[dict[str, str]] = []
    if kunde_telefon_normalized:
        ext_props.append({"id": ga_prop_id("kunde_telefon"), "value": kunde_telefon_normalized})
    if kunde_email:
        ext_props.append({"id": ga_prop_id("kunde_email"), "value": kunde_email})
    if idempotency_key:
        ext_props.append({"id": ga_prop_id("ga_ref"), "value": idempotency_key})
    if ext_props:
        body["singleValueExtendedProperties"] = ext_props

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
# EVENT-FIND (kunde_telefon / kunde_email — Storno-Suche)
# ---------------------------------------------------------------------

async def find_events(
    tenant_id: UUID,
    *,
    time_min: dt.datetime,
    time_max: dt.datetime,
    kunde_telefon_normalized: str | None = None,
    kunde_email: str | None = None,
    employee_id: UUID | None = None,
) -> list[dict[str, Any]]:
    """Sucht Events nach Telefon ODER Email im Zeitraum.

    Strategie:
    1. Metadaten-Suche: /me/calendarView mit $filter ueber
       singleValueExtendedProperties (Property-Set GA_PROPSET_GUID).
       Wir machen pro Kriterium einen Request (kein OR im $filter
       weil $expand-Klausel sonst kollidiert).
    2. Volltext-Fallback: /me/events?$search="..." (mit
       ConsistencyLevel: eventual). Manuelles Datum-Filtering weil
       $search und $filter nicht kombinierbar sind.

    Returns: Liste von dicts wie im Adapter dokumentiert.
    """
    from plugins.kalender.event_match import (
        verify_fulltext_phone_match, verify_fulltext_email_match,
    )
    token = await get_microsoft_token(tenant_id, employee_id=employee_id)
    start_iso = _iso_no_tz(time_min)
    end_iso = _iso_no_tz(time_max)

    results: dict[str, dict[str, Any]] = {}

    def _parse_dt(s: str) -> dt.datetime:
        return dt.datetime.fromisoformat(s).replace(tzinfo=None)

    def _ingest(items, *, source, phone_match, email_match):
        for ev in items or []:
            eid = ev.get("id") or ""
            if not eid:
                continue
            try:
                s_dt = _parse_dt(ev["start"]["dateTime"])
                e_dt = _parse_dt(ev["end"]["dateTime"])
            except (KeyError, ValueError):
                continue
            if eid in results:
                if phone_match:
                    results[eid]["kunde_telefon_match"] = True
                if email_match:
                    results[eid]["kunde_email_match"] = True
                continue
            results[eid] = {
                "event_id": eid,
                "start_dt": s_dt,
                "end_dt": e_dt,
                "summary": (ev.get("subject") or "").strip(),
                "description": _strip_html(
                    (ev.get("body") or {}).get("content") or ""
                ) or (ev.get("bodyPreview") or "").strip(),
                "location": (
                    (ev.get("location") or {}).get("displayName") or ""
                ).strip(),
                "kunde_telefon_match": bool(phone_match),
                "kunde_email_match": bool(email_match),
                "match_source": source,
            }

    async def _query_metadata(prop_name: str, prop_value: str,
                              phone_match: bool, email_match: bool):
        prop_id = ga_prop_id(prop_name)
        # $filter erfordert ConsistencyLevel: eventual fuer extended
        # property queries (Graph "advanced query parameters").
        params = {
            "startDateTime": start_iso,
            "endDateTime": end_iso,
            "$filter": (
                f"singleValueExtendedProperties/any("
                f"ep:ep/id eq '{prop_id}' and ep/value eq '{prop_value}')"
            ),
            "$expand": (
                f"singleValueExtendedProperties($filter=id eq '{prop_id}')"
            ),
            "$top": 100,
        }
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
                resp = await client.get(
                    f"{GRAPH_API_BASE}/me/calendarView",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "ConsistencyLevel": "eventual",
                        "Prefer": f'outlook.timezone="{DEFAULT_TIMEZONE}"',
                    },
                    params=params,
                )
                if resp.status_code != 200:
                    logger.warning(
                        f"Outlook find_events metadata {prop_name} "
                        f"{resp.status_code}: {resp.text[:200]}"
                    )
                    return
                data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Outlook find_events metadata {prop_name} crash: {exc}")
            return
        _ingest(data.get("value", []), source="metadata",
                phone_match=phone_match, email_match=email_match)

    # Volltext-Fallback ueber calendarView: Graph's `$search` ist auf
    # `/me/events` schlicht nicht supported (HTTP 501 "SearchEvents:
    # The parameter $search is not currently supported on the Events
    # resource"). Stattdessen holen wir EINMAL alle Events im Zeitraum
    # und filtern lokal — gegen body/content (verify_fn) UND attendees[]
    # (typisch Outlook: Kunde als Meeting-Attendee eingeladen, Mail im
    # body steht dann nirgends).
    fulltext_cache: list[list[dict]] = []  # mutable closure-Wrapper

    async def _fetch_all_events_in_range() -> list[dict]:
        if fulltext_cache:
            return fulltext_cache[0]
        params = {
            "startDateTime": start_iso,
            "endDateTime": end_iso,
            "$select": "id,subject,start,end,location,body,bodyPreview,attendees",
            "$top": 250,
            "$orderby": "start/dateTime",
        }
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
                resp = await client.get(
                    f"{GRAPH_API_BASE}/me/calendarView",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Prefer": f'outlook.timezone="{DEFAULT_TIMEZONE}"',
                    },
                    params=params,
                )
                if resp.status_code != 200:
                    logger.warning(
                        f"Outlook find_events fulltext-list "
                        f"{resp.status_code}: {resp.text[:200]}"
                    )
                    fulltext_cache.append([])
                    return []
                data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Outlook find_events fulltext-list crash: {exc}")
            fulltext_cache.append([])
            return []
        events = data.get("value", [])
        fulltext_cache.append(events)
        return events

    async def _query_fulltext_phone(verify_needle: str):
        events = await _fetch_all_events_in_range()
        verified = [
            ev for ev in events
            if verify_fulltext_phone_match(
                verify_needle,
                _strip_html((ev.get("body") or {}).get("content") or ""),
            )
        ]
        _ingest(verified, source="fulltext",
                phone_match=True, email_match=False)

    async def _query_fulltext_email(needle_email_lower: str):
        events = await _fetch_all_events_in_range()
        verified = []
        for ev in events:
            body_text = _strip_html(
                (ev.get("body") or {}).get("content") or ""
            )
            if verify_fulltext_email_match(needle_email_lower, body_text):
                verified.append(ev)
                continue
            # Outlook-spezifisch: Kunde als Attendee eingeladen
            # (Mail steht dann oft NICHT im Body).
            if email_in_attendees(ev, needle_email_lower):
                verified.append(ev)
        _ingest(verified, source="fulltext",
                phone_match=False, email_match=True)

    if kunde_telefon_normalized:
        await _query_metadata(
            "kunde_telefon", kunde_telefon_normalized,
            phone_match=True, email_match=False,
        )
    if kunde_email:
        await _query_metadata(
            "kunde_email", kunde_email,
            phone_match=False, email_match=True,
        )
    if kunde_telefon_normalized:
        await _query_fulltext_phone(kunde_telefon_normalized)
    if kunde_email:
        await _query_fulltext_email(kunde_email)

    return list(results.values())


def _strip_html(html: str) -> str:
    """Minimaler HTML-Stripper fuer body.content — entfernt Tags ohne
    Library-Dependency. Reicht weil wir nur Substring-Matching wollen."""
    if not html:
        return ""
    import re
    text = re.sub(r"<[^>]+>", " ", html)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return " ".join(text.split()).strip()


def email_in_attendees(event: dict, email_lower: str) -> bool:
    """True wenn der Outlook-Event den Kunden als Attendee mit der
    gesuchten Mailadresse listet.

    Wird vom find_events-Volltext-Pfad genutzt: in Outlook-Terminen
    steht die Kunden-Mail haeufig NICHT im Body, sondern nur in
    attendees[].emailAddress.address (Meeting-Einladung). Pure dict-
    Lookup-Logik damit testbar ohne HTTP-Mock.
    """
    if not email_lower or not event:
        return False
    for att in (event.get("attendees") or []):
        addr = ((att.get("emailAddress") or {}).get("address") or "").lower()
        if addr == email_lower:
            return True
    return False


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
