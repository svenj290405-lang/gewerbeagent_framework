"""Regressions-Test fuer GoogleCalendarAdapter._rfc3339.

Bug: ``time.isoformat() + self._tz_offset()`` haengte den festen Offset
``+02:00`` auch an bereits tz-aware Zeitstempel an. Der anfrage_reminder_cron
uebergibt UTC-aware Datetimes -> ``.isoformat()`` traegt ``+00:00`` schon
selbst -> Ergebnis ``...+00:00+02:00`` (doppelter Offset) -> Google Calendar
v3 antwortete stuendlich mit HTTP 400 ``Bad Request`` (find_events/freebusy).

_rfc3339 darf daher den Offset NUR an naive Werte anhaengen.
"""
from __future__ import annotations

import datetime as dt
import re
import uuid

from plugins.kalender.adapters import GoogleCalendarAdapter

# Zwei Offsets hintereinander, z.B. "+00:00+02:00" oder "+00:00Z".
_DOUBLE_OFFSET = re.compile(r"[+-]\d{2}:\d{2}[+-]\d{2}:\d{2}$")


def _adapter() -> GoogleCalendarAdapter:
    # __init__ ist netz-/OAuth-frei; _rfc3339 braucht nur self._tz_offset().
    return GoogleCalendarAdapter(
        tenant_id=uuid.uuid4(), calendar_id="primary", employee_id=None
    )


def test_rfc3339_tz_aware_keeps_single_offset():
    """tz-aware (UTC) -> genau ein Offset, KEIN doppeltes Anhaengen."""
    aware = dt.datetime(2026, 5, 26, 6, 36, tzinfo=dt.timezone.utc)
    out = _adapter()._rfc3339(aware)
    assert out == "2026-05-26T06:36:00+00:00"
    assert not _DOUBLE_OFFSET.search(out), f"doppelter Offset: {out!r}"


def test_rfc3339_tz_aware_non_utc_keeps_own_offset():
    """tz-aware mit Nicht-UTC-Offset -> eigener Offset bleibt erhalten."""
    tz = dt.timezone(dt.timedelta(hours=2))
    aware = dt.datetime(2026, 5, 26, 8, 36, tzinfo=tz)
    out = _adapter()._rfc3339(aware)
    assert out == "2026-05-26T08:36:00+02:00"
    assert not _DOUBLE_OFFSET.search(out)


def test_rfc3339_naive_gets_local_offset_appended():
    """naive -> als lokale Zeit interpretiert, konfigurierter Offset dran."""
    naive = dt.datetime(2026, 5, 26, 8, 36)
    out = _adapter()._rfc3339(naive)
    assert out == "2026-05-26T08:36:00+02:00"
    assert not _DOUBLE_OFFSET.search(out)
