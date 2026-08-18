"""Tests fuer die Rechte-Endpunkte im Team-Screen
(core/api/app_screens.py: /team/{slug}/rechte, /rolle, /recht).

Reine Unit-Tests mit Fakes — keine DB (Muster wie test_app_verbindungen.py).
Die Endpoint-Coroutinen werden direkt aufgerufen, die FastAPI-Depends sind
nur Funktionsparameter.

Schwerpunkt sind die Guards: ohne sie koennte sich ein Inhaber aus dem
eigenen Betrieb aussperren oder eine Buerokraft sich selbst hochstufen.
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from core.api import app_screens
from core.features import permissions as P


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------

def _req(body=None, tenant_id=None, aktueller=None):
    req = SimpleNamespace()

    async def _json():
        return body or {}

    req.json = _json
    req.state = SimpleNamespace(
        app_tenant=SimpleNamespace(id=tenant_id or uuid.uuid4(), slug="pilot"),
        app_employee=aktueller or SimpleNamespace(
            id=uuid.uuid4(), slug="inhaber", is_default=True,
        ),
        app_is_inhaber=True,
    )
    return req


def _emp(slug="henrik", rolle=P.ROLLE_BUERO, is_default=False, emp_id=None):
    return SimpleNamespace(
        id=emp_id or uuid.uuid4(), slug=slug, name=slug.title(),
        role=rolle, is_default=is_default,
    )


def _body(res):
    return json.loads(res.body)


def _patch_lookup(monkeypatch, emp):
    async def _fake(tid, slug):
        return emp
    monkeypatch.setattr(app_screens, "_get_employee_by_slug", _fake)


# --------------------------------------------------------------------------
# GET /team/{slug}/rechte
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rechte_zeigt_herkunft(monkeypatch):
    emp = _emp(rolle=P.ROLLE_BUERO)
    _patch_lookup(monkeypatch, emp)

    async def _keine_overrides(_id):
        return {}
    monkeypatch.setattr(
        "core.features.permission_check.overrides_fuer_employee", _keine_overrides,
    )

    res = await app_screens.api_team_rechte("henrik", _req(), _e=None)
    b = _body(res)

    assert b["ok"] is True
    assert b["rolle"] == P.ROLLE_BUERO
    assert len(b["rechte"]) == len(P.RECHTE)
    assert {r["key"] for r in b["rollen"]} == set(P.ALLE_ROLLEN)

    nach_key = {r["key"]: r for r in b["rechte"]}
    # aus der Rolle geerbt
    assert nach_key["auftraege.alle_sehen"]["aus_rolle"] is True
    assert nach_key["auftraege.alle_sehen"]["effektiv"] is True
    assert nach_key["auftraege.alle_sehen"]["override"] is None
    # nicht in der Rolle
    assert nach_key["buchhaltung.sehen"]["aus_rolle"] is False
    assert nach_key["buchhaltung.sehen"]["effektiv"] is False
    # nicht delegierbar wird als solches markiert
    assert nach_key["team.rechte"]["delegierbar"] is False


@pytest.mark.asyncio
async def test_rechte_unbekannter_mitarbeiter_404(monkeypatch):
    _patch_lookup(monkeypatch, None)
    res = await app_screens.api_team_rechte("gibtsnicht", _req(), _e=None)
    assert res.status_code == 404


# --------------------------------------------------------------------------
# POST /team/{slug}/rolle — Guards
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rolle_setzen_happy_path(monkeypatch):
    emp = _emp(rolle=P.ROLLE_BUERO)
    _patch_lookup(monkeypatch, emp)
    gesetzt = {}

    async def _set(emp_id, rolle):
        gesetzt["emp_id"], gesetzt["rolle"] = emp_id, rolle
        return True
    monkeypatch.setattr("core.features.permission_check.set_rolle", _set)

    res = await app_screens.api_team_set_rolle(
        "henrik", _req({"rolle": P.ROLLE_MONTEUR}), _e=None, _c=None,
    )
    assert res.status_code == 200
    assert _body(res)["rolle"] == P.ROLLE_MONTEUR
    assert gesetzt["rolle"] == P.ROLLE_MONTEUR


@pytest.mark.asyncio
async def test_rolle_eigene_nicht_aenderbar(monkeypatch):
    """Sonst stuft sich der Inhaber selbst herunter und kommt nicht zurueck."""
    ich = SimpleNamespace(id=uuid.uuid4(), slug="inhaber", is_default=True)
    _patch_lookup(monkeypatch, ich)
    res = await app_screens.api_team_set_rolle(
        "inhaber", _req({"rolle": P.ROLLE_MONTEUR}, aktueller=ich), _e=None, _c=None,
    )
    assert res.status_code == 400
    assert "eigene" in _body(res)["error"].lower()


@pytest.mark.asyncio
async def test_rolle_des_inhaber_accounts_bleibt(monkeypatch):
    _patch_lookup(monkeypatch, _emp(slug="chef", is_default=True))
    res = await app_screens.api_team_set_rolle(
        "chef", _req({"rolle": P.ROLLE_MONTEUR}), _e=None, _c=None,
    )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_inhaber_rolle_nicht_vergebbar(monkeypatch):
    _patch_lookup(monkeypatch, _emp())
    res = await app_screens.api_team_set_rolle(
        "henrik", _req({"rolle": P.ROLLE_INHABER}), _e=None, _c=None,
    )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_unbekannte_rolle_400(monkeypatch):
    _patch_lookup(monkeypatch, _emp())
    res = await app_screens.api_team_set_rolle(
        "henrik", _req({"rolle": "kapitaen"}), _e=None, _c=None,
    )
    assert res.status_code == 400


# --------------------------------------------------------------------------
# POST /team/{slug}/recht — Guards
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_recht_gewaehren(monkeypatch):
    _patch_lookup(monkeypatch, _emp())
    gesehen = {}

    async def _set(emp_id, tid, key, allowed):
        gesehen.update(key=key, allowed=allowed)
        return True
    monkeypatch.setattr("core.features.permission_check.set_override", _set)

    res = await app_screens.api_team_set_recht(
        "henrik", _req({"key": "buchhaltung.sehen", "allowed": True}),
        _e=None, _c=None,
    )
    assert res.status_code == 200
    assert gesehen == {"key": "buchhaltung.sehen", "allowed": True}


@pytest.mark.asyncio
async def test_recht_zuruecksetzen_mit_null(monkeypatch):
    _patch_lookup(monkeypatch, _emp())
    gesehen = {}

    async def _set(emp_id, tid, key, allowed):
        gesehen.update(key=key, allowed=allowed)
        return True
    monkeypatch.setattr("core.features.permission_check.set_override", _set)

    res = await app_screens.api_team_set_recht(
        "henrik", _req({"key": "auftraege.alle_sehen", "allowed": None}),
        _e=None, _c=None,
    )
    assert res.status_code == 200
    assert gesehen["allowed"] is None


@pytest.mark.asyncio
async def test_nicht_delegierbares_recht_wird_abgelehnt(monkeypatch):
    """Der zentrale Schutz gegen Rechteausweitung: wer 'Rechte vergeben'
    bekaeme, koennte sich anschliessend alles selbst zuschalten."""
    _patch_lookup(monkeypatch, _emp())
    res = await app_screens.api_team_set_recht(
        "henrik", _req({"key": "team.rechte", "allowed": True}),
        _e=None, _c=None,
    )
    assert res.status_code == 400
    res2 = await app_screens.api_team_set_recht(
        "henrik", _req({"key": "einstellungen.verwalten", "allowed": True}),
        _e=None, _c=None,
    )
    assert res2.status_code == 400


@pytest.mark.asyncio
async def test_eigene_rechte_nicht_aenderbar(monkeypatch):
    ich = SimpleNamespace(id=uuid.uuid4(), slug="buero", is_default=False)
    _patch_lookup(monkeypatch, ich)
    res = await app_screens.api_team_set_recht(
        "buero", _req({"key": "buchhaltung.sehen", "allowed": True}, aktueller=ich),
        _e=None, _c=None,
    )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_unbekanntes_recht_400(monkeypatch):
    _patch_lookup(monkeypatch, _emp())
    res = await app_screens.api_team_set_recht(
        "henrik", _req({"key": "quatsch", "allowed": True}), _e=None, _c=None,
    )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_allowed_muss_bool_oder_null_sein(monkeypatch):
    _patch_lookup(monkeypatch, _emp())
    res = await app_screens.api_team_set_recht(
        "henrik", _req({"key": "buchhaltung.sehen", "allowed": "ja"}),
        _e=None, _c=None,
    )
    assert res.status_code == 400
