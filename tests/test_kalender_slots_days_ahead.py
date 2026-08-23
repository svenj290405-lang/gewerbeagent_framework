"""Tests fuer die beiden Aufruf-Modi von kalender._find_free_slots.

Hintergrund: die PWA (/termine/freie-slots, /verbindungen/kalender/test)
und der Q-Assistent rufen mit ``days_ahead`` auf, der Handler kannte aber
nur ``datum``/``uhrzeit``. Ergebnis war ein ValueError im pauschalen
except -> eine Antwort OHNE ``slots``-Key -> alle Aufrufer machten
``out.get("slots") or []`` und zeigten stumm eine leere Liste. Der
Diagnose-Screen meldete dabei sogar "ok".

Getestet wird mit einem Fake-Adapter (kein Google/Microsoft noetig):
  - Tage-Modus liefert Slots
  - Wunschtermin-Modus verhaelt sich unveraendert
  - Fehlerpfad liefert erfolg=False UND einen slots-Key
  - Slots am heutigen Tag liegen nie in der Vergangenheit
"""
from __future__ import annotations

import datetime as dt
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from plugins.kalender import handler as kal


# =====================================================================
# Test-Doubles
# =====================================================================

class _FakeAdapter:
    """Kalender-Adapter, in dem nie etwas belegt ist."""

    provider_name = "fake"

    def __init__(self, busy=None):
        self._busy = busy or []

    async def get_busy_periods(self, time_min, time_max):
        return self._busy


def _make_plugin(arbeitstage=(0, 1, 2, 3, 4)):
    """Plugin-Instanz mit echtem PluginContext (tenant_id/config sind
    read-only Properties auf BasePlugin)."""
    from core.plugin_system import PluginContext

    ctx = PluginContext(
        tenant_id=uuid.uuid4(),
        tenant_slug="testbetrieb",
        config={
            "betrieb_name": "Testbetrieb",
            "calendar_id": "primary",
            "arbeitszeiten_start": "08:00",
            "arbeitszeiten_ende": "17:00",
            "arbeitstage": list(arbeitstage),
            "termin_dauer_minuten": 60,
            "zeitzone": "Europe/Berlin",
        },
    )
    return kal.Plugin(ctx)


def _patch_adapter(adapter):
    return patch.object(
        kal, "get_calendar_adapter",
        new=lambda *a, **kw: _async_return(adapter),
    )


async def _async_return(value):
    return value


# =====================================================================
# Tage-Modus (der reparierte Pfad)
# =====================================================================

@pytest.mark.asyncio
async def test_days_ahead_liefert_slots():
    p = _make_plugin()
    with _patch_adapter(_FakeAdapter()):
        out = await p._find_free_slots({"days_ahead": 7})

    assert out["erfolg"] is True
    assert out["anzahl"] > 0, "days_ahead lieferte frueher immer 0 Slots"
    assert len(out["slots"]) == out["anzahl"]
    for s in out["slots"]:
        assert {"datum", "uhrzeit", "wochentag"} <= set(s)


@pytest.mark.asyncio
async def test_days_ahead_deckelt_auf_max_slots():
    p = _make_plugin()
    with _patch_adapter(_FakeAdapter()):
        out = await p._find_free_slots({"days_ahead": 999})

    assert out["erfolg"] is True
    assert len(out["slots"]) <= kal.MAX_SLOTS


@pytest.mark.asyncio
async def test_days_ahead_unlesbar_faellt_auf_default():
    p = _make_plugin()
    with _patch_adapter(_FakeAdapter()):
        out = await p._find_free_slots({"days_ahead": "viele"})

    assert out["erfolg"] is True
    assert out["anzahl"] > 0


# =====================================================================
# Wunschtermin-Modus bleibt unveraendert
# =====================================================================

@pytest.mark.asyncio
async def test_wunschtermin_modus_unveraendert():
    p = _make_plugin()
    ziel = dt.date.today() + dt.timedelta(days=30)
    while ziel.weekday() not in p.config["arbeitstage"]:
        ziel += dt.timedelta(days=1)

    with _patch_adapter(_FakeAdapter()):
        out = await p._find_free_slots({
            "datum": ziel.strftime("%d.%m.%Y"), "uhrzeit": "10:00",
        })

    assert out["erfolg"] is True
    assert out["anzahl"] > 0
    # Der Wunschtag muss unter den Vorschlaegen sein.
    assert any(s["datum"] == ziel.strftime("%d.%m.%Y") for s in out["slots"])


# =====================================================================
# Fehlerpfad: erfolg=False, aber slots-Key vorhanden
# =====================================================================

@pytest.mark.asyncio
async def test_fehlerpfad_liefert_slots_key():
    """Ohne slots-Key kann ein Aufrufer 'nichts frei' nicht von
    'Aufruf kaputt' unterscheiden — genau daran hing das falsche Gruen."""
    p = _make_plugin()
    with _patch_adapter(_FakeAdapter()):
        out = await p._find_free_slots({"datum": "quatsch", "uhrzeit": "10:00"})

    assert out["erfolg"] is False
    assert "slots" in out, "slots-Key fehlt im Fehlerpfad"
    assert out["slots"] == []
    assert out["anzahl"] == 0
    assert out.get("nachricht")


# =====================================================================
# Keine Slots in der Vergangenheit
# =====================================================================

@pytest.mark.asyncio
async def test_heute_keine_vergangenen_slots():
    # Arbeitstage auf "alle" setzen, damit der Test an jedem Wochentag laeuft.
    p = _make_plugin(arbeitstage=(0, 1, 2, 3, 4, 5, 6))
    jetzt = dt.datetime.now()

    with _patch_adapter(_FakeAdapter()):
        out = await p._find_free_slots({"days_ahead": 1})

    heute = jetzt.strftime("%d.%m.%Y")
    for s in out["slots"]:
        if s["datum"] != heute:
            continue
        h, m = map(int, s["uhrzeit"].split(":"))
        slot_dt = jetzt.replace(hour=h, minute=m, second=0, microsecond=0)
        assert slot_dt >= jetzt, f"Slot {s} liegt in der Vergangenheit"


# =====================================================================
# Wunschdatum in der Vergangenheit
# =====================================================================
# Live aufgefallen: eine Suche zum 25.05. lieferte am 23.08. bereitwillig
# sechs Slots — am 25.05., also drei Monate in der Vergangenheit. Q haette
# einem Kunden Termine angeboten, die laengst vorbei sind. Ein fehlendes
# Jahr in der Mail ("am 22.05.") oder ein verhoertes Datum am Telefon
# reicht dafuer aus.

@pytest.mark.asyncio
async def test_wunschdatum_in_der_vergangenheit_wird_vorgezogen():
    p = _make_plugin(arbeitstage=(0, 1, 2, 3, 4, 5, 6))
    vergangen = dt.date.today() - dt.timedelta(days=90)
    with _patch_adapter(_FakeAdapter()):
        out = await p._find_free_slots({
            "datum": vergangen.strftime("%d.%m.%Y"), "uhrzeit": "10:00",
        })

    assert out["erfolg"] is True
    assert out["slots"], "Es sollen Slots kommen — nur eben zukuenftige"
    assert out.get("hinweis_vergangenheit") is True
    heute = dt.date.today()
    for s in out["slots"]:
        tag = dt.datetime.strptime(s["datum"], "%d.%m.%Y").date()
        assert tag >= heute, f"Slot in der Vergangenheit: {s['datum']}"


@pytest.mark.asyncio
async def test_zukuenftiges_wunschdatum_bleibt_unangetastet():
    p = _make_plugin(arbeitstage=(0, 1, 2, 3, 4, 5, 6))
    ziel = dt.date.today() + dt.timedelta(days=10)
    with _patch_adapter(_FakeAdapter()):
        out = await p._find_free_slots({
            "datum": ziel.strftime("%d.%m.%Y"), "uhrzeit": "10:00",
        })

    assert out["erfolg"] is True
    assert out.get("hinweis_vergangenheit") is False
    assert any(s["datum"] == ziel.strftime("%d.%m.%Y") for s in out["slots"]), (
        "Der gewuenschte Tag selbst sollte unter den Vorschlaegen sein"
    )


@pytest.mark.asyncio
async def test_buchung_in_der_vergangenheit_wird_abgelehnt():
    p = _make_plugin(arbeitstage=(0, 1, 2, 3, 4, 5, 6))
    vergangen = dt.date.today() - dt.timedelta(days=7)
    with _patch_adapter(_FakeAdapter()):
        out = await p._book_appointment({
            "datum": vergangen.strftime("%d.%m.%Y"), "uhrzeit": "10:00",
            "kunde_name": "Test Kunde", "kunde_telefon": "0651 123",
            "anliegen": "Beratung",
        })

    assert out["erfolg"] is False
    assert out.get("grund") == "termin_in_vergangenheit"
