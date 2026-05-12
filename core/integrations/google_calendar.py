"""Google-Calendar-Integration — leichtgewichtiges Event-Listing.

Bewusst direkt mit httpx + Access-Token statt googleapiclient.discovery
(wie google_drive.py macht), damit wir keinen sync→async-Wrapper brauchen
und der Aufruf-Pfad ueberblickbar bleibt. Nutzt den gleichen
provider="google"-OAuth-Token wie /drive_verbinden.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any
from uuid import UUID

import httpx

from core.database import AsyncSessionLocal
from core.models import OAuthToken
from core.security.oauth_token_lookup import find_oauth_token

logger = logging.getLogger(__name__)

GOOGLE_CAL_BASE = "https://www.googleapis.com/calendar/v3"
HTTP_TIMEOUT_SECONDS = 10.0
DEFAULT_TIMEZONE = "Europe/Berlin"


async def _ensure_fresh_access_token(oauth_token: OAuthToken) -> str:
    """Stellt sicher dass access_token gueltig ist — refreshed sonst.

    Returns: gueltiger access_token-String.
    """
    from datetime import datetime, timezone
    from sqlalchemy import select
    from google.auth.transport.requests import Request as GRequest
    from google.oauth2.credentials import Credentials
    from plugins.kalender.google_auth import _get_google_client_creds

    scopes = (oauth_token.scopes or "").split(",")
    client_id, client_secret = _get_google_client_creds()

    # Race-Schutz: SELECT FOR UPDATE auf der Token-Zeile
    async with AsyncSessionLocal() as session:
        row = (await session.execute(
            select(OAuthToken).where(OAuthToken.id == oauth_token.id)
            .with_for_update()
        )).scalar_one()
        creds = Credentials(
            token=row.access_token,
            refresh_token=row.refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=scopes,
        )
        now = datetime.now(timezone.utc)
        fresh = (
            row.access_token_expires_at is not None
            and row.access_token_expires_at > now
            and row.access_token
        )
        if not fresh and creds.refresh_token:
            creds.refresh(GRequest())
            row.access_token = creds.token
            if creds.expiry:
                row.access_token_expires_at = creds.expiry.replace(
                    tzinfo=timezone.utc,
                )
            await session.commit()
        return row.access_token


async def list_events_for_day(
    tenant_id: UUID,
    target_date: dt.date,
    employee_id: UUID | None = None,
    calendar_id: str = "primary",
) -> list[dict[str, Any]]:
    """Alle Google-Calendar-Events des Tages.

    Returns: Liste von dicts mit Keys:
      - start_dt, end_dt (datetime, naive)
      - location (str)
      - subject (str) — Event-Titel
      - event_id (str) — Google-Event-ID
      - body_preview (str) — Description (gekuerzt)

    Bei fehlendem Token: leere Liste (failsafe — Caller merkt es im
    /briefing als "keine Events").
    """
    oauth_token = await find_oauth_token(tenant_id, "google", employee_id)
    if oauth_token is None:
        logger.info(
            f"Google-Calendar: kein OAuth-Token fuer tenant={tenant_id} "
            f"employee={employee_id}"
        )
        return []

    try:
        access_token = await _ensure_fresh_access_token(oauth_token)
    except Exception as exc:
        logger.exception(f"Google-Token-Refresh gescheitert: {exc}")
        return []

    # Tages-Intervall in ISO (mit Z fuer UTC). Wir gehen vom Lokal-Tag
    # aus, also 00:00 Europe/Berlin bis 23:59:59 Europe/Berlin.
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(DEFAULT_TIMEZONE)
    except Exception:
        from datetime import timezone as _tz
        tz = _tz.utc
    day_start = dt.datetime.combine(target_date, dt.time(0, 0, 0)).replace(tzinfo=tz)
    day_end = dt.datetime.combine(target_date, dt.time(23, 59, 59)).replace(tzinfo=tz)
    time_min = day_start.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    time_max = day_end.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")

    url = f"{GOOGLE_CAL_BASE}/calendars/{calendar_id}/events"
    params = {
        "timeMin": time_min,
        "timeMax": time_max,
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": "200",
    }

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {access_token}"},
                params=params,
            )
            if resp.status_code != 200:
                logger.warning(
                    f"Google-Calendar list_events {resp.status_code}: "
                    f"{resp.text[:200]}"
                )
                return []
            data = resp.json()
    except Exception as exc:
        logger.exception(f"Google-Calendar HTTP gescheitert: {exc}")
        return []

    events: list[dict[str, Any]] = []
    for ev in data.get("items", []):
        try:
            s_data = ev.get("start") or {}
            e_data = ev.get("end") or {}
            # All-day events haben "date" statt "dateTime" — die ueberspringen
            s_iso = s_data.get("dateTime")
            e_iso = e_data.get("dateTime")
            if not s_iso or not e_iso:
                continue
            s_dt = dt.datetime.fromisoformat(s_iso)
            e_dt = dt.datetime.fromisoformat(e_iso)
            # In lokale Zeitzone wandeln + naive (so wie Microsoft-Variante)
            if s_dt.tzinfo is not None:
                s_dt = s_dt.astimezone(tz).replace(tzinfo=None)
            if e_dt.tzinfo is not None:
                e_dt = e_dt.astimezone(tz).replace(tzinfo=None)
            events.append({
                "start_dt": s_dt,
                "end_dt": e_dt,
                "location": (ev.get("location") or "").strip(),
                "subject": (ev.get("summary") or "").strip(),
                "event_id": ev.get("id") or "",
                "body_preview": (ev.get("description") or "")[:300].strip(),
            })
        except Exception as exc:
            logger.warning(f"Skipping malformed Google event: {exc}")
    logger.info(
        f"Google-Calendar list_events tenant={tenant_id} date={target_date}: "
        f"{len(events)} events"
    )
    return events
