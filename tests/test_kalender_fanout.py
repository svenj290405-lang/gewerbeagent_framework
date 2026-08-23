"""Tests fuer die verteilte Slot-Suche und die Spiegelbuchung.

Bis hierher fragte _find_free_slots genau EINEN Kalender, und die Slots
trugen keine employee_id — deshalb buchte alles ausser dem Telefon-Pfad
faktisch in den Kalender des Inhabers.

Getestet mit Fake-Adaptern pro Mitarbeiter (kein Google/Microsoft):
  - Slots tragen den Mitarbeiter
  - Entdopplung pro Zeitpunkt (nicht dreimal "Di 10:00")
  - Der Inhaber kommt zuletzt dran
  - Mitarbeiter ohne verbundenen Kalender fallen raus
  - Eigene Arbeitszeiten schlagen den Tenant-Default
  - Der Spiegel im Inhaber-Kalender ist "frei" markiert
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
    """Nie belegt; protokolliert angelegte Events."""

    provider_name = "fake"

    def __init__(self, name="?"):
        self.name = name
        self.events: list[dict] = []

    async def get_busy_periods(self, time_min, time_max):
        return []

    async def create_event(self, **kw):
        self.events.append(kw)
        return {"id": f"ev-{len(self.events)}", "html_link": ""}


def _emp(name, slug=None, is_default=False, provider="google",
         arbeitszeiten=None, arbeitstage=None):
    return SimpleNamespace(
        id=uuid.uuid4(), name=name, slug=slug or name.lower(),
        is_default=is_default, calendar_provider=provider,
        arbeitszeiten=arbeitszeiten, arbeitstage=arbeitstage,
        tenant_id=None,
    )


def _make_plugin():
    from core.plugin_system import PluginContext

    ctx = PluginContext(
        tenant_id=uuid.uuid4(), tenant_slug="testbetrieb",
        config={
            "betrieb_name": "Testbetrieb", "calendar_id": "primary",
            "arbeitszeiten_start": "08:00", "arbeitszeiten_ende": "17:00",
            "arbeitstage": [0, 1, 2, 3, 4, 5, 6],
            "termin_dauer_minuten": 60, "zeitzone": "Europe/Berlin",
        },
    )
    return kal.Plugin(ctx)


def _patch_kandidaten(p, employees, adapter_je_emp):
    """Kandidaten-Ermittlung und Adapter-Factory ersetzen."""
    async def _kandidaten(employee_id, anker):
        # zweiter Wert = Grund; "ok" heisst normaler Fan-Out
        return employees, "ok"

    async def _adapter(tenant_id, employee_id=None, fallback_calendar_id=None):
        return adapter_je_emp[str(employee_id)]

    return (
        patch.object(p, "_slot_kandidaten", _kandidaten),
        patch.object(kal, "get_calendar_adapter", _adapter),
    )


# =====================================================================
# Fan-Out
# =====================================================================

@pytest.mark.asyncio
async def test_slots_tragen_den_mitarbeiter():
    p = _make_plugin()
    marco = _emp("Marco")
    adapter = {str(marco.id): _FakeAdapter("marco")}
    p1, p2 = _patch_kandidaten(p, [marco], adapter)
    with p1, p2:
        out = await p._find_free_slots({"days_ahead": 3})

    assert out["erfolg"] is True
    assert out["anzahl"] > 0
    for s in out["slots"]:
        assert s["employee_id"] == str(marco.id)
        assert s["employee_name"] == "Marco"


@pytest.mark.asyncio
async def test_entdopplung_pro_zeitpunkt():
    """Bei drei freien Mitarbeitern soll der Kunde nicht dreimal
    denselben Zeitpunkt angeboten bekommen."""
    p = _make_plugin()
    emps = [_emp("Marco"), _emp("Henrik"), _emp("Luca")]
    adapter = {str(e.id): _FakeAdapter(e.name) for e in emps}
    p1, p2 = _patch_kandidaten(p, emps, adapter)
    with p1, p2:
        out = await p._find_free_slots({"days_ahead": 3})

    zeitpunkte = [(s["datum"], s["uhrzeit"]) for s in out["slots"]]
    assert len(zeitpunkte) == len(set(zeitpunkte)), "Zeitpunkt doppelt angeboten"


@pytest.mark.asyncio
async def test_inhaber_kommt_zuletzt():
    """Der Chef springt nur ein, wenn sonst niemand kann."""
    p = _make_plugin()
    chef = _emp("Chef", is_default=True)
    monteur = _emp("Marco")
    adapter = {str(e.id): _FakeAdapter(e.name) for e in (chef, monteur)}
    p1, p2 = _patch_kandidaten(p, [chef, monteur], adapter)
    with p1, p2:
        out = await p._find_free_slots({"days_ahead": 2})

    assert out["slots"], "keine Slots"
    # Bei voller Verfuegbarkeit beider gehen alle Zeitpunkte an den
    # Nicht-Inhaber.
    assert all(s["employee_name"] == "Marco" for s in out["slots"])


@pytest.mark.asyncio
async def test_ohne_verbundenen_kalender_kein_fanout():
    """Fallback auf den Tenant-Kalender, wenn niemand einen eigenen hat —
    Verhalten wie vor dem Fan-Out."""
    p = _make_plugin()

    async def _keine(employee_id, anker):
        # leer + "ok" = niemand hat einen Kalender -> Betriebskalender.
        # (Der Fall "abwesend" darf NICHT ausweichen, siehe eigener Test.)
        return [], "ok"

    async def _adapter(*a, **kw):
        return _FakeAdapter("tenant")

    with patch.object(p, "_slot_kandidaten", _keine), \
         patch.object(kal, "get_calendar_adapter", _adapter):
        out = await p._find_free_slots({"days_ahead": 3})

    assert out["erfolg"] is True
    assert out["anzahl"] > 0
    assert all(s["employee_id"] is None for s in out["slots"])


@pytest.mark.asyncio
async def test_eigene_arbeitszeiten_schlagen_tenant_default():
    """Ein Fruehschichtler bekam bisher Slots bis 17 Uhr angeboten."""
    p = _make_plugin()
    frueh = _emp("Frueh", arbeitszeiten={"start": "06:00", "end": "10:00"})
    adapter = {str(frueh.id): _FakeAdapter("frueh")}
    p1, p2 = _patch_kandidaten(p, [frueh], adapter)
    with p1, p2:
        out = await p._find_free_slots({"days_ahead": 5})

    assert out["slots"], "keine Slots"
    for s in out["slots"]:
        stunde = int(s["uhrzeit"].split(":")[0])
        assert 6 <= stunde < 10, f"{s['uhrzeit']} liegt ausserhalb der Schicht"


@pytest.mark.asyncio
async def test_eigene_arbeitstage_werden_beachtet():
    p = _make_plugin()
    # arbeitet nur montags
    nur_mo = _emp("Montags", arbeitstage=[0])
    adapter = {str(nur_mo.id): _FakeAdapter("mo")}
    p1, p2 = _patch_kandidaten(p, [nur_mo], adapter)
    with p1, p2:
        out = await p._find_free_slots({"days_ahead": 14})

    for s in out["slots"]:
        tag = dt.datetime.strptime(s["datum"], "%d.%m.%Y")
        assert tag.weekday() == 0, f"{s['datum']} ist kein Montag"


# =====================================================================
# Spiegelbuchung
# =====================================================================

@pytest.mark.asyncio
async def test_spiegel_ist_als_frei_markiert():
    """Sonst blockiert die Betriebsuebersicht die eigene Slot-Suche des
    Inhabers und er ist rechnerisch dauerhaft ausgebucht."""
    p = _make_plugin()
    chef = _emp("Chef", is_default=True)
    monteur = _emp("Marco")
    chef_adapter = _FakeAdapter("chef")

    async def _default_emp(tenant_id):
        return chef

    async def _adapter(tenant_id, employee_id=None, fallback_calendar_id=None):
        return chef_adapter

    with patch("core.models.employee.get_default_employee", _default_emp), \
         patch.object(kal, "get_calendar_adapter", _adapter):
        await p._spiegel_termin(
            employee_id=monteur.id, summary="Heizung Mueller",
            description="…", location="Musterweg 1",
            start=dt.datetime(2026, 9, 1, 10, 0),
            ende=dt.datetime(2026, 9, 1, 11, 0),
            idempotency_key="abc123",
        )

    assert len(chef_adapter.events) == 1
    ev = chef_adapter.events[0]
    assert ev["transparent"] is True
    assert ev["zusatz_props"] == {"ga_mirror": "1"}
    assert ev["summary"].startswith("[Team] ")


@pytest.mark.asyncio
async def test_kein_spiegel_wenn_termin_schon_beim_chef_liegt():
    p = _make_plugin()
    chef = _emp("Chef", is_default=True)
    chef_adapter = _FakeAdapter("chef")

    async def _default_emp(tenant_id):
        return chef

    async def _adapter(*a, **kw):
        return chef_adapter

    with patch("core.models.employee.get_default_employee", _default_emp), \
         patch.object(kal, "get_calendar_adapter", _adapter):
        await p._spiegel_termin(
            employee_id=chef.id, summary="X", description="", location="",
            start=dt.datetime(2026, 9, 1, 10, 0),
            ende=dt.datetime(2026, 9, 1, 11, 0),
            idempotency_key=None,
        )

    assert chef_adapter.events == []


@pytest.mark.asyncio
async def test_spiegel_fehler_bricht_buchung_nicht():
    """Eine fehlende Betriebsuebersicht ist aergerlich, ein verlorener
    Kundentermin waere schlimm."""
    p = _make_plugin()

    async def _kaputt(tenant_id):
        raise RuntimeError("DB weg")

    with patch("core.models.employee.get_default_employee", _kaputt):
        # darf NICHT werfen
        await p._spiegel_termin(
            employee_id=uuid.uuid4(), summary="X", description="",
            location="", start=dt.datetime(2026, 9, 1, 10, 0),
            ende=dt.datetime(2026, 9, 1, 11, 0), idempotency_key=None,
        )


@pytest.mark.asyncio
async def test_abwesender_mitarbeiter_bekommt_keine_ersatz_slots():
    """Ist der ausdruecklich angefragte Mitarbeiter weg, gibt es KEINE Slots.

    Der Rueckfall auf den Betriebskalender ist hier falsch: die Slots
    kaemen aus einem fremden Kalender, wuerden dem Kunden aber als
    Termine dieses Mitarbeiters angeboten — mitten in dessen Urlaub.
    """
    p = _make_plugin()

    async def _abwesend(employee_id, anker):
        return [], "abwesend"

    async def _adapter(*a, **kw):
        raise AssertionError("Es darf gar kein Kalender geoeffnet werden")

    with patch.object(p, "_slot_kandidaten", _abwesend), \
         patch.object(kal, "get_calendar_adapter", _adapter):
        out = await p._find_free_slots({"datum": "26.08.2026", "uhrzeit": "10:00"})

    assert out["erfolg"] is True
    assert out["slots"] == []
    assert out["grund"] == "mitarbeiter_abwesend"
