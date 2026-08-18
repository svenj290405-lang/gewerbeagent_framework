"""Tests fuer die Rechte-Registry und ihre Aufloesung.

Reine Unit-Tests mit Fakes, keine DB (gleiches Muster wie
test_app_auth.py und test_automations.py).

Die Invarianten-Tests sind der eigentliche Wert: sie halten die Registry
klein und fail-closed, auch wenn spaeter jemand einen Key ergaenzt.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from core.features import permission_check as pc
from core.features import permissions as P


# =====================================================================
# Fakes
# =====================================================================

def _emp(rolle=P.ROLLE_MONTEUR, is_default=False):
    return SimpleNamespace(
        id=uuid.uuid4(), role=rolle, is_default=is_default,
    )


@pytest.fixture(autouse=True)
def _leerer_cache():
    pc.invalidate_permission_cache()
    yield
    pc.invalidate_permission_cache()


# =====================================================================
# Registry-Invarianten
# =====================================================================

def test_inhaber_hat_alle_rechte():
    assert P.ROLLEN_PRESETS[P.ROLLE_INHABER] == P.ALLE_RECHTE


def test_jeder_key_wird_von_mindestens_einem_preset_verweigert():
    """Sonst ist der Key ein Zombie: niemand kann ihn je entziehen, und
    er blaeht nur die Rechte-Oberflaeche auf."""
    for key in P.RECHTE:
        verweigert = [
            rolle for rolle, preset in P.ROLLEN_PRESETS.items()
            if key not in preset
        ]
        assert verweigert, (
            f"{key!r} ist in JEDEM Preset enthalten — dann braucht es "
            f"kein eigenes Recht zu sein."
        )


def test_presets_enthalten_nur_bekannte_keys():
    for rolle, preset in P.ROLLEN_PRESETS.items():
        unbekannt = preset - P.ALLE_RECHTE
        assert not unbekannt, f"Rolle {rolle}: unbekannte Keys {unbekannt}"


def test_nicht_delegierbare_keys_nur_beim_inhaber():
    """Wer 'Rechte vergeben' bekommt, kann sich alles andere selbst
    geben — das darf keine Nicht-Inhaber-Rolle standardmaessig haben."""
    for key, recht in P.RECHTE.items():
        if not recht.nicht_delegierbar:
            continue
        for rolle, preset in P.ROLLEN_PRESETS.items():
            if rolle == P.ROLLE_INHABER:
                continue
            assert key not in preset, (
                f"{key!r} ist nicht delegierbar, steckt aber im Preset "
                f"von {rolle!r}"
            )


def test_key_und_dict_schluessel_stimmen_ueberein():
    for key, recht in P.RECHTE.items():
        assert recht.key == key


def test_jedes_recht_hat_label_und_gruppe():
    for recht in P.RECHTE.values():
        assert recht.label.strip()
        assert recht.description.strip()
        assert recht.gruppe.strip()


def test_rechte_nach_gruppe_ist_vollstaendig():
    gruppiert = P.rechte_nach_gruppe()
    summe = sum(len(v) for v in gruppiert.values())
    assert summe == len(P.RECHTE)


# =====================================================================
# Rollen-Aufloesung
# =====================================================================

def test_unbekannte_rolle_faellt_auf_restriktivste():
    assert P.rechte_fuer_rolle("kapitaen") == P.ROLLEN_PRESETS[P.ROLLE_DEFAULT]
    assert P.rechte_fuer_rolle(None) == P.ROLLEN_PRESETS[P.ROLLE_DEFAULT]


def test_monteur_sieht_keine_buchhaltung():
    assert "buchhaltung.sehen" not in P.rechte_fuer_rolle(P.ROLLE_MONTEUR)
    assert "auftraege.alle_sehen" not in P.rechte_fuer_rolle(P.ROLLE_MONTEUR)


def test_buero_sieht_keine_buchhaltung_aber_alle_auftraege():
    buero = P.rechte_fuer_rolle(P.ROLLE_BUERO)
    assert "buchhaltung.sehen" not in buero
    assert "buchhaltung.fuehren" not in buero
    assert "auftraege.alle_sehen" in buero
    assert "einstellungen.verwalten" not in buero


# =====================================================================
# Effektive Rechte (Preset +/- Override)
# =====================================================================

@pytest.mark.asyncio
async def test_inhaber_kurzschluss_ohne_db():
    """Der Inhaber bekommt alles, BEVOR die DB gefragt wird — ein
    Datenbankproblem darf ihn nie aus seinem Betrieb aussperren."""
    rechte = await pc.rechte_fuer_employee(_emp(is_default=True))
    assert rechte == P.ALLE_RECHTE


@pytest.mark.asyncio
async def test_preset_greift_ohne_overrides(monkeypatch):
    _keine_overrides(monkeypatch)
    rechte = await pc.rechte_fuer_employee(_emp(P.ROLLE_BUERO))
    assert rechte == P.ROLLEN_PRESETS[P.ROLLE_BUERO]


@pytest.mark.asyncio
async def test_override_gewaehrt_zusaetzliches_recht(monkeypatch):
    _overrides(monkeypatch, [("buchhaltung.sehen", True)])
    rechte = await pc.rechte_fuer_employee(_emp(P.ROLLE_MONTEUR))
    assert "buchhaltung.sehen" in rechte


@pytest.mark.asyncio
async def test_override_entzieht_recht_aus_der_rolle(monkeypatch):
    _overrides(monkeypatch, [("auftraege.alle_sehen", False)])
    rechte = await pc.rechte_fuer_employee(_emp(P.ROLLE_BUERO))
    assert "auftraege.alle_sehen" not in rechte
    # der Rest der Rolle bleibt
    assert "kunden.pflegen" in rechte


@pytest.mark.asyncio
async def test_nicht_delegierbares_recht_rutscht_nicht_per_override_durch(monkeypatch):
    """Doppelt gesichert: der Schreibpfad lehnt es ab, aber eine von Hand
    gesetzte Zeile darf hier ebenfalls nicht wirken."""
    _overrides(monkeypatch, [("team.rechte", True)])
    rechte = await pc.rechte_fuer_employee(_emp(P.ROLLE_BUERO))
    assert "team.rechte" not in rechte


@pytest.mark.asyncio
async def test_verwaister_key_wird_ignoriert(monkeypatch):
    _overrides(monkeypatch, [("gibts.nicht.mehr", True)])
    rechte = await pc.rechte_fuer_employee(_emp(P.ROLLE_BUERO))
    assert rechte == P.ROLLEN_PRESETS[P.ROLLE_BUERO]


@pytest.mark.asyncio
async def test_db_fehler_faellt_auf_preset_zurueck(monkeypatch):
    """Fehlt die Tabelle noch (Code deployed, Migration nicht), soll die
    Rollen-Vorlage greifen statt ein 500."""
    class _Kaputt:
        async def __aenter__(self): raise RuntimeError("relation does not exist")
        async def __aexit__(self, *a): return False

    monkeypatch.setattr(pc, "AsyncSessionLocal", lambda: _Kaputt())
    rechte = await pc.rechte_fuer_employee(_emp(P.ROLLE_BUERO))
    assert rechte == P.ROLLEN_PRESETS[P.ROLLE_BUERO]


@pytest.mark.asyncio
async def test_hat_recht_unbekannter_key_ist_false():
    assert await pc.hat_recht(_emp(is_default=True), "quatsch.key") is False


@pytest.mark.asyncio
async def test_cache_wird_invalidiert(monkeypatch):
    emp = _emp(P.ROLLE_MONTEUR)
    _overrides(monkeypatch, [])
    erst = await pc.rechte_fuer_employee(emp)
    assert "buchhaltung.sehen" not in erst

    _overrides(monkeypatch, [("buchhaltung.sehen", True)])
    # ohne Invalidierung liefert der Cache noch den alten Stand
    assert await pc.rechte_fuer_employee(emp) == erst

    pc.invalidate_permission_cache(emp.id)
    assert "buchhaltung.sehen" in await pc.rechte_fuer_employee(emp)


# =====================================================================
# Schreibpfad-Guards
# =====================================================================

@pytest.mark.asyncio
async def test_set_override_lehnt_nicht_delegierbares_ab():
    ok = await pc.set_override(uuid.uuid4(), uuid.uuid4(), "team.rechte", True)
    assert ok is False


@pytest.mark.asyncio
async def test_set_override_lehnt_unbekannten_key_ab():
    ok = await pc.set_override(uuid.uuid4(), uuid.uuid4(), "quatsch", True)
    assert ok is False


@pytest.mark.asyncio
async def test_set_rolle_lehnt_ungueltige_rolle_ab():
    assert await pc.set_rolle(uuid.uuid4(), "kapitaen") is False


def test_rolle_oder_default():
    assert pc.rolle_oder_default(_emp(is_default=True)) == P.ROLLE_INHABER
    assert pc.rolle_oder_default(_emp(P.ROLLE_BUERO)) == P.ROLLE_BUERO
    assert pc.rolle_oder_default(_emp("quatsch")) == P.ROLLE_DEFAULT


# =====================================================================
# Helfer: DB-Zeilen faken
# =====================================================================

def _overrides(monkeypatch, rows):
    class _Result:
        def all(self): return list(rows)

    class _Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def execute(self, *a, **kw): return _Result()

    monkeypatch.setattr(pc, "AsyncSessionLocal", lambda: _Session())


def _keine_overrides(monkeypatch):
    _overrides(monkeypatch, [])
