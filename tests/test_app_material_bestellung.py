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


@pytest.fixture(autouse=True)
def _material_freigeschaltet(monkeypatch):
    """Der Bereich haengt seit dem Audit am 2026-08-24 am Feature-Schalter
    "material" — vorher war der Schalter wirkungslos. Fuer die Tests hier
    ist er an; dass er wirkt, prueft der Test ganz unten."""
    async def _an(_request):
        return True
    monkeypatch.setattr(app_screens, "_material_aktiv", _an)


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


# =====================================================================
# POST /material/anlegen — Link-Pruefung
# =====================================================================

@pytest.mark.asyncio
@pytest.mark.parametrize("link", [
    "javascript:alert(1)",          # kein Web-Link -> landet in window.open()
    "shop.example/spax",            # Schema vergessen
    "data:text/html,<script>1</script>",
])
async def test_anlegen_lehnt_nicht_web_links_ab(link):
    resp = await app_screens.api_material_anlegen(
        request=_req({"name": "Spax", "bestell_link": link}), _e=None, _c=None,
    )
    assert resp.status_code == 400
    assert "http" in _json_body(resp)["error"]


@pytest.mark.asyncio
async def test_anlegen_lehnt_zu_langen_link_ab():
    resp = await app_screens.api_material_anlegen(
        request=_req({"name": "Spax", "bestell_link": "https://x.de/" + "a" * 2000}),
        _e=None, _c=None,
    )
    assert resp.status_code == 400
    assert "lang" in _json_body(resp)["error"]


# =====================================================================
# Q-Tools (core/ai/command_center.py)
# =====================================================================

def _q_ctx(tid=None):
    from core.ai import command_center as cc
    tenant = SimpleNamespace(id=tid or uuid.uuid4(), slug="pilot",
                             company_name="Jantos GmbH")
    return cc.Ctx(tenant=tenant, employee=_emp(), tid=tenant.id)


class _FakeScalarSession:
    """Session, die auf jedes execute() denselben Skalar liefert."""

    def __init__(self, wert):
        self.wert = wert

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: self.wert)


@pytest.mark.asyncio
async def test_q_bestaetigung_zeigt_namen_statt_uuid(monkeypatch):
    """Gemini liefert nur die material_id — der Nutzer darf trotzdem keine
    UUID zur Bestaetigung vorgelegt bekommen."""
    from core.ai import command_center as cc
    import core.database.connection as conn

    monkeypatch.setattr(conn, "get_session",
                        lambda: _FakeScalarSession("Spax-Schrauben"))
    mid = str(uuid.uuid4())
    zeile = await cc._summary_material(_q_ctx(), {"material_id": mid, "menge": 4})
    assert zeile == "4× Spax-Schrauben bestellen?"
    assert mid not in zeile


@pytest.mark.asyncio
async def test_q_bestaetigung_faellt_auf_material_zurueck(monkeypatch):
    """Unbekannte/fremde ID: lieber das neutrale Wort als die UUID."""
    from core.ai import command_center as cc
    import core.database.connection as conn

    monkeypatch.setattr(conn, "get_session", lambda: _FakeScalarSession(None))
    zeile = await cc._summary_material(_q_ctx(), {"material_id": str(uuid.uuid4())})
    assert zeile == "Material bestellen?"


@pytest.mark.asyncio
async def test_q_material_anlegen_lehnt_nicht_web_link_ab():
    from core.ai import command_center as cc

    res = await cc._run_material_anlegen(
        _q_ctx(), {"name": "Spax", "bestell_link": "javascript:alert(1)"})
    assert res["ok"] is False
    assert "http" in res["error"]


@pytest.mark.asyncio
async def test_q_bestellhistorie_liefert_eintraege(monkeypatch):
    from core.ai import command_center as cc
    import core.database.connection as conn
    import datetime as dt

    rows = [
        SimpleNamespace(material_name="Spax", menge=5, einheit="Packung",
                        created_at=dt.datetime(2026, 8, 21, 9, 30)),
        SimpleNamespace(material_name="Dübel", menge=2, einheit="Stück",
                        created_at=None),
    ]
    monkeypatch.setattr(conn, "get_session", lambda: _FakeListSession(rows))
    res = await cc._run_material_bestellungen(_q_ctx(), {"anzahl": 99})
    assert [b["material"] for b in res["bestellungen"]] == ["Spax", "Dübel"]
    assert res["bestellungen"][0]["zeit"].startswith("2026-08-21")
    assert res["bestellungen"][1]["zeit"] is None


@pytest.mark.asyncio
async def test_abgeschaltetes_material_ist_wirklich_abgeschaltet(monkeypatch):
    """Der Admin-Schalter "Material-Bestellungen" bewirkte gar nichts: es
    gab im ganzen Code keine Pruefung darauf. In der App aenderte sich beim
    Abschalten nichts, und Q bestellte weiter."""
    async def _aus(_request):
        return False
    monkeypatch.setattr(app_screens, "_material_aktiv", _aus)

    request = _req()
    for aufruf in (
        app_screens.api_material_list(request=request, _e=None),
        app_screens.api_material_bestellungen(request=request, _e=None),
    ):
        antwort = await aufruf
        assert antwort.status_code == 403
        assert "nicht aktiv" in json.loads(antwort.body)["error"]
