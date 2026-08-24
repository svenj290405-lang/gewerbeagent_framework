"""Zwei Schutzmechanismen rund um automatische Terminbuchungen.

1. **FreeBusy-Guard** (`_suche_slots_am_tag`): sucht jemand abends noch
   einen Termin fuer HEUTE, ist der Rest-Tag kuerzer als die Termindauer
   — es gibt also gar keinen Kandidaten mehr. Vorher lief die FreeBusy-
   Abfrage trotzdem los und Google quittierte das leere Intervall mit
   HTTP 400 'timeRangeEmpty'. Gefangen wurde der Fehler, bezahlt haben
   wir den Aufruf trotzdem.

2. **Mengen-Deckel** (`termin_throttle`): eine Kundenmail kann ohne
   Rueckfrage in den Kalender schreiben. Die Einzelfall-Gates waren eng,
   die MENGE war ungedeckelt — mit genug Wegwerf-Adressen liess sich der
   Kalender vollbuchen.
"""
from __future__ import annotations

import datetime as dt
import uuid
from unittest.mock import patch

import pytest

from plugins.kalender import handler as kal


# =====================================================================
# 1) FreeBusy-Guard
# =====================================================================

class _ZaehlenderAdapter:
    """Adapter, der mitzaehlt, wie oft FreeBusy wirklich gefragt wurde."""

    provider_name = "fake"

    def __init__(self):
        self.freebusy_calls: list[tuple] = []

    async def get_busy_periods(self, time_min, time_max):
        self.freebusy_calls.append((time_min, time_max))
        return []


def _make_plugin(start="08:00", ende="17:00"):
    from core.plugin_system import PluginContext

    ctx = PluginContext(
        tenant_id=uuid.uuid4(),
        tenant_slug="testbetrieb",
        config={
            "betrieb_name": "Testbetrieb",
            "calendar_id": "primary",
            "arbeitszeiten_start": start,
            "arbeitszeiten_ende": ende,
            "arbeitstage": [0, 1, 2, 3, 4, 5, 6],
            "termin_dauer_minuten": 60,
            "zeitzone": "Europe/Berlin",
        },
    )
    return kal.Plugin(ctx)


@pytest.mark.asyncio
async def test_kein_freebusy_call_wenn_termin_laenger_als_der_arbeitstag():
    """Termindauer > Arbeitszeit-Fenster: es gibt keinen einzigen
    Kandidaten, also darf auch nicht nach Belegung gefragt werden."""
    plugin = _make_plugin(start="08:00", ende="17:00")
    adapter = _ZaehlenderAdapter()

    slots = await plugin._suche_slots_am_tag(
        adapter,
        target_date=dt.date.today() + dt.timedelta(days=3),
        wunsch_uhrzeit_anker=None,
        max_count=3,
        dauer=600,   # 10 h in einem 9-h-Tag
    )

    assert slots == []
    assert adapter.freebusy_calls == [], (
        "FreeBusy wurde ohne einen einzigen Slot-Kandidaten abgefragt — "
        "genau der 'timeRangeEmpty'-400er, den der Guard verhindert"
    )


@pytest.mark.asyncio
async def test_kein_freebusy_call_wenn_der_rest_tag_schon_vorbei_ist():
    """Der Abend-Fall: fuer HEUTE liegt der fruehestmoegliche Start
    hinter dem Arbeitsende. Ueber einen kuenstlich langen Vorlauf
    deterministisch nachgestellt (statt an der Systemuhr zu drehen —
    die Methode importiert datetime lokal, ein Modul-Patch greift da
    nicht)."""
    plugin = _make_plugin(start="08:00", ende="17:00")
    adapter = _ZaehlenderAdapter()

    with patch.object(kal, "SLOT_VORLAUF_MINUTEN", 60 * 24):
        slots = await plugin._suche_slots_am_tag(
            adapter,
            target_date=dt.date.today(),
            wunsch_uhrzeit_anker=None,
            max_count=3,
            dauer=60,
        )

    assert slots == []
    assert adapter.freebusy_calls == []


@pytest.mark.asyncio
async def test_freebusy_laeuft_wenn_der_tag_noch_platz_hat():
    """Gegenprobe: an einem normalen Tag wird weiterhin gefragt."""
    plugin = _make_plugin()
    adapter = _ZaehlenderAdapter()

    slots = await plugin._suche_slots_am_tag(
        adapter,
        target_date=dt.date.today() + dt.timedelta(days=3),
        wunsch_uhrzeit_anker=None,
        max_count=3,
        dauer=60,
    )

    assert len(adapter.freebusy_calls) == 1
    assert slots, "an einem freien Tag muss es Vorschlaege geben"


# =====================================================================
# 2) Mengen-Deckel fuer Mail-Buchungen
# =====================================================================

@pytest.mark.asyncio
async def test_stunden_deckel_greift():
    from core.integrations import termin_throttle as tt

    async def _fake_count(*, tenant_id, window_hours):
        return 4 if window_hours == 1 else 4

    with patch.object(tt, "count_mail_bookings", _fake_count):
        throttled, grund = await tt.should_throttle_booking(
            tenant_id=uuid.uuid4(),
        )

    assert throttled is True
    assert grund == "stunden-deckel"


@pytest.mark.asyncio
async def test_tages_deckel_greift_auch_bei_ruhiger_stunde():
    from core.integrations import termin_throttle as tt

    async def _fake_count(*, tenant_id, window_hours):
        # Letzte Stunde ruhig, der Tag aber voll — der Schwarm kam
        # ueber den Vormittag verteilt.
        return 0 if window_hours == 1 else 12

    with patch.object(tt, "count_mail_bookings", _fake_count):
        throttled, grund = await tt.should_throttle_booking(
            tenant_id=uuid.uuid4(),
        )

    assert throttled is True
    assert grund == "tages-deckel"


@pytest.mark.asyncio
async def test_normalbetrieb_wird_nicht_gedeckelt():
    from core.integrations import termin_throttle as tt

    async def _fake_count(*, tenant_id, window_hours):
        return 1 if window_hours == 1 else 5

    with patch.object(tt, "count_mail_bookings", _fake_count):
        throttled, grund = await tt.should_throttle_booking(
            tenant_id=uuid.uuid4(),
        )

    assert throttled is False
    assert grund is None


def test_deckel_schwellwerte_sind_code_konstanten():
    """Der Deckel ist Missbrauchsschutz, keine Einstellung — er darf
    nicht ueber ToolConfig/DB verstellbar sein."""
    from core.integrations import termin_throttle as tt

    assert isinstance(tt.MAX_MAIL_BOOKINGS_PER_TENANT_PER_DAY, int)
    assert isinstance(tt.MAX_MAIL_BOOKINGS_PER_TENANT_PER_HOUR, int)
    assert (
        tt.MAX_MAIL_BOOKINGS_PER_TENANT_PER_HOUR
        < tt.MAX_MAIL_BOOKINGS_PER_TENANT_PER_DAY
    )
