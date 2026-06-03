"""Tests fuer Material-Bestellungen der PWA
(core/api/app_screens.py: /material/{id}/bestellen + /material/bestellungen).

Reine Unit-Tests mit Fakes — keine echte DB (Muster wie test_app_diktat.py).

Deckt:
- bestellen: ungueltige id -> 400, nicht gefunden -> 404, deaktiviert -> 409
- bestellen Happy-Path: Audit-Log mit employee_id + bestell_art="link",
  Menge default = standard_menge, explizite Menge wird uebernommen,
  Antwort enthaelt den bestell_link
- bestellungen: Mapping der Historie
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from core.api import app_screens


class _FakeMatSession:
    def __init__(self, material):
        self.material = material
        self.added = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: self.material)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        pass


class _FakeListSession:
    def __init__(self, rows):
        self.rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: self.rows))


def _material(aktiv=True):
    return SimpleNamespace(
        id=uuid.uuid4(), name="Spax-Schrauben", bestell_link="https://shop.example/spax",
        einheit="Packung", standard_menge=5, aktiv=aktiv,
    )


def _req(body=None, tenant_id=None):
    req = SimpleNamespace()

    async def _json():
        if body is None:
            raise ValueError("no body")
        return body
    req.json = _json
    req.state = SimpleNamespace(app_tenant=SimpleNamespace(id=tenant_id or uuid.uuid4()))
    return req


def _emp():
    return SimpleNamespace(id=uuid.uuid4())


def _json_body(resp):
    return json.loads(bytes(resp.body))


# =====================================================================
# POST /material/{id}/bestellen
# =====================================================================

@pytest.mark.asyncio
async def test_bestellen_invalid_id_returns_400(monkeypatch):
    monkeypatch.setattr(app_screens, "get_session", lambda: _FakeMatSession(None))
    resp = await app_screens.api_material_bestellen(
        mid="kein-uuid", request=_req({}), emp=_emp(), _c=None,
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_bestellen_not_found_returns_404(monkeypatch):
    monkeypatch.setattr(app_screens, "get_session", lambda: _FakeMatSession(None))
    resp = await app_screens.api_material_bestellen(
        mid=str(uuid.uuid4()), request=_req({}), emp=_emp(), _c=None,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_bestellen_inactive_returns_409(monkeypatch):
    sess = _FakeMatSession(_material(aktiv=False))
    monkeypatch.setattr(app_screens, "get_session", lambda: sess)
    resp = await app_screens.api_material_bestellen(
        mid=str(uuid.uuid4()), request=_req({}), emp=_emp(), _c=None,
    )
    assert resp.status_code == 409
    assert sess.added == []   # kein Log bei deaktiviertem Material


@pytest.mark.asyncio
async def test_bestellen_happy_path_logs_and_returns_link(monkeypatch):
    mat = _material()
    sess = _FakeMatSession(mat)
    monkeypatch.setattr(app_screens, "get_session", lambda: sess)
    emp = _emp()
    resp = await app_screens.api_material_bestellen(
        mid=str(uuid.uuid4()), request=_req({}), emp=emp, _c=None,
    )
    assert resp.status_code == 200
    j = _json_body(resp)
    assert j["ok"] is True
    assert j["bestell_link"] == "https://shop.example/spax"
    assert len(sess.added) == 1
    log = sess.added[0]
    assert log.employee_id == emp.id
    assert log.material_name == "Spax-Schrauben"
    assert log.bestell_art == "link"
    assert log.menge == 5            # default = standard_menge


@pytest.mark.asyncio
async def test_bestellen_explicit_menge(monkeypatch):
    sess = _FakeMatSession(_material())
    monkeypatch.setattr(app_screens, "get_session", lambda: sess)
    resp = await app_screens.api_material_bestellen(
        mid=str(uuid.uuid4()), request=_req({"menge": 3}), emp=_emp(), _c=None,
    )
    assert resp.status_code == 200
    assert sess.added[0].menge == 3


# =====================================================================
# GET /material/bestellungen
# =====================================================================

@pytest.mark.asyncio
async def test_bestellungen_history_mapping(monkeypatch):
    rows = [
        SimpleNamespace(id=uuid.uuid4(), material_name="Spax", menge=5,
                        einheit="Packung", created_at=None),
        SimpleNamespace(id=uuid.uuid4(), material_name="Dübel", menge=2,
                        einheit="Stück", created_at=None),
    ]
    monkeypatch.setattr(app_screens, "get_session", lambda: _FakeListSession(rows))
    resp = await app_screens.api_material_bestellungen(request=_req(), _e=None)
    j = _json_body(resp)
    assert [b["material"] for b in j["bestellungen"]] == ["Spax", "Dübel"]
    assert j["bestellungen"][0]["menge"] == 5
    assert j["bestellungen"][1]["einheit"] == "Stück"
