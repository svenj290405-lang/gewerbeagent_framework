"""Tests fuer das Aufträge-Lifecycle-Board der PWA
(core/api/app_screens.py: /auftraege + /auftraege/{id}/status).

Reine Unit-Tests mit Fakes — keine echte DB (Muster wie test_app_diktat.py).

Deckt:
- GET /auftraege: Mapping (Lifecycle-Index, abgebrochen-Flag)
- POST status: ungueltige id -> 400, nicht-setzbarer Status -> 400
  (besonders rechnung_gesendet = Geld-Pfad), nicht gefunden -> 404
- POST status Happy-Path: accepted setzt accepted_at; andere Stati nur status
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from core.api import app_screens


class _FakeObjSession:
    def __init__(self, obj):
        self.obj = obj
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: self.obj)

    async def commit(self):
        self.committed = True


class _FakeListSession:
    def __init__(self, rows):
        self.rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt):
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: self.rows)
        )


def _req(body=None, tenant_id=None):
    req = SimpleNamespace()

    async def _json():
        return body or {}
    req.json = _json
    req.state = SimpleNamespace(app_tenant=SimpleNamespace(id=tenant_id or uuid.uuid4()))
    return req


def _json_body(resp):
    return json.loads(bytes(resp.body))


# =====================================================================
# GET /auftraege
# =====================================================================

@pytest.mark.asyncio
async def test_auftraege_list_maps_lifecycle(monkeypatch):
    rows = [
        SimpleNamespace(id=uuid.uuid4(), kunde_name="Mueller",
                        gesamtbetrag_brutto_eur=1000, status="accepted", created_at=None),
        SimpleNamespace(id=uuid.uuid4(), kunde_name="Schmidt",
                        gesamtbetrag_brutto_eur=None, status="abgebrochen", created_at=None),
    ]
    monkeypatch.setattr(app_screens, "get_session", lambda: _FakeListSession(rows))
    resp = await app_screens.api_auftraege(request=_req(), _e=None)
    j = _json_body(resp)
    a0, a1 = j["auftraege"]
    assert a0["kunde"] == "Mueller"
    assert a0["status"] == "accepted"
    assert a0["schritt"] == 1            # accepted ist Index 1 im Lifecycle
    assert a0["abgebrochen"] is False
    assert a1["abgebrochen"] is True
    assert a1["schritt"] is None         # abgebrochen ist nicht im Lifecycle


# =====================================================================
# POST /auftraege/{id}/status
# =====================================================================

@pytest.mark.asyncio
async def test_status_invalid_id_returns_400(monkeypatch):
    monkeypatch.setattr(app_screens, "get_session", lambda: _FakeObjSession(None))
    resp = await app_screens.api_auftrag_status(
        angebot_id="not-a-uuid", request=_req({"status": "accepted"}), _e=None, _c=None,
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_status_rejects_non_settable(monkeypatch):
    # rechnung_gesendet ist der Geld-Pfad und darf hier NICHT gesetzt werden
    monkeypatch.setattr(app_screens, "get_session", lambda: _FakeObjSession(SimpleNamespace()))
    resp = await app_screens.api_auftrag_status(
        angebot_id=str(uuid.uuid4()),
        request=_req({"status": "rechnung_gesendet"}), _e=None, _c=None,
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_status_rejects_bogus(monkeypatch):
    monkeypatch.setattr(app_screens, "get_session", lambda: _FakeObjSession(SimpleNamespace()))
    resp = await app_screens.api_auftrag_status(
        angebot_id=str(uuid.uuid4()),
        request=_req({"status": "voellig_erfunden"}), _e=None, _c=None,
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_status_not_found_returns_404(monkeypatch):
    monkeypatch.setattr(app_screens, "get_session", lambda: _FakeObjSession(None))
    resp = await app_screens.api_auftrag_status(
        angebot_id=str(uuid.uuid4()),
        request=_req({"status": "arbeit_laeuft"}), _e=None, _c=None,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_status_accepted_sets_accepted_at(monkeypatch):
    ang = SimpleNamespace(status="rechnung_erstellt", accepted_at=None)
    sess = _FakeObjSession(ang)
    monkeypatch.setattr(app_screens, "get_session", lambda: sess)
    resp = await app_screens.api_auftrag_status(
        angebot_id=str(uuid.uuid4()),
        request=_req({"status": "accepted"}), _e=None, _c=None,
    )
    assert resp.status_code == 200
    assert ang.status == "accepted"
    assert ang.accepted_at is not None
    assert sess.committed is True


@pytest.mark.asyncio
async def test_status_progress_does_not_touch_accepted_at(monkeypatch):
    existing_ts = "schon-gesetzt"
    ang = SimpleNamespace(status="accepted", accepted_at=existing_ts)
    monkeypatch.setattr(app_screens, "get_session", lambda: _FakeObjSession(ang))
    resp = await app_screens.api_auftrag_status(
        angebot_id=str(uuid.uuid4()),
        request=_req({"status": "arbeit_laeuft"}), _e=None, _c=None,
    )
    assert resp.status_code == 200
    assert ang.status == "arbeit_laeuft"
    assert ang.accepted_at == existing_ts   # unveraendert
