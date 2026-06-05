"""Tests für den "Aktuelles"-Flow der PWA (core/api/app_screens.py):
/aktuelles, /beratung/{id}/entscheidung, /auftraege/{id}/fortschritt,
/rechnung/vorbereiten, /rechnung/senden.

Reine Unit-Tests mit Fakes — keine echte DB (Muster wie test_app_auftraege.py).
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from core.api import app_screens
from core.models.angebot import (
    ANGEBOT_STATUS_WORK_DONE,
    ANGEBOT_STATUS_WORK_IN_PROGRESS,
)
from core.models.kundengespraech import (
    KUNDENGESPRAECH_STATUS_ABGELEHNT,
    KUNDENGESPRAECH_STATUS_ANGENOMMEN,
)


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


def _req(body=None, tenant_id=None, query=None):
    req = SimpleNamespace()

    async def _json():
        return body or {}
    req.json = _json
    req.state = SimpleNamespace(app_tenant=SimpleNamespace(id=tenant_id or uuid.uuid4()))
    req.query_params = query or {}
    return req


def _body(resp):
    return json.loads(bytes(resp.body))


# ===================== /aktuelles =====================

@pytest.mark.asyncio
async def test_aktuelles_aggregates(monkeypatch):
    async def _rr(tid):
        return [{"id": "1", "kunde": "A", "telefon": "0", "anliegen": ""}]

    async def _ber(tid):
        return [{"id": "2", "kunde": "B", "briefing": "", "termin": "", "termin_iso": None}]

    async def _auf(tid):
        return [{"id": "3", "kunde": "C", "status": "arbeit_laeuft", "in_arbeit": True}]
    monkeypatch.setattr(app_screens, "_open_rueckrufe", _rr)
    monkeypatch.setattr(app_screens, "_beratung_leads", _ber)
    monkeypatch.setattr(app_screens, "_aktuelle_auftraege", _auf)
    resp = await app_screens.api_aktuelles(request=_req(), _e=None)
    j = _body(resp)
    assert [x["kunde"] for x in j["rueckrufe"]] == ["A"]
    assert [x["kunde"] for x in j["beratung"]] == ["B"]
    assert [x["kunde"] for x in j["auftraege"]] == ["C"]


# ===================== /beratung/{id}/entscheidung =====================

@pytest.mark.asyncio
async def test_beratung_invalid_id_400(monkeypatch):
    monkeypatch.setattr(app_screens, "get_session", lambda: _FakeObjSession(None))
    resp = await app_screens.api_beratung_entscheidung(
        gespraech_id="nope", request=_req({"entscheidung": "annehmen"}), _e=None, _c=None)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_beratung_bad_entscheidung_400(monkeypatch):
    monkeypatch.setattr(app_screens, "get_session", lambda: _FakeObjSession(SimpleNamespace()))
    resp = await app_screens.api_beratung_entscheidung(
        gespraech_id=str(uuid.uuid4()), request=_req({"entscheidung": "vielleicht"}), _e=None, _c=None)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_beratung_not_found_404(monkeypatch):
    monkeypatch.setattr(app_screens, "get_session", lambda: _FakeObjSession(None))
    resp = await app_screens.api_beratung_entscheidung(
        gespraech_id=str(uuid.uuid4()), request=_req({"entscheidung": "annehmen"}), _e=None, _c=None)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_beratung_annehmen_sets_status(monkeypatch):
    k = SimpleNamespace(kunde_name="Meier", status="erfasst")
    sess = _FakeObjSession(k)
    monkeypatch.setattr(app_screens, "get_session", lambda: sess)
    resp = await app_screens.api_beratung_entscheidung(
        gespraech_id=str(uuid.uuid4()), request=_req({"entscheidung": "annehmen"}), _e=None, _c=None)
    assert resp.status_code == 200
    assert k.status == KUNDENGESPRAECH_STATUS_ANGENOMMEN
    assert sess.committed is True


@pytest.mark.asyncio
async def test_beratung_ablehnen_soft_deletes(monkeypatch):
    k = SimpleNamespace(kunde_name="Meier", status="erfasst")
    monkeypatch.setattr(app_screens, "get_session", lambda: _FakeObjSession(k))
    resp = await app_screens.api_beratung_entscheidung(
        gespraech_id=str(uuid.uuid4()), request=_req({"entscheidung": "ablehnen"}), _e=None, _c=None)
    assert resp.status_code == 200
    assert k.status == KUNDENGESPRAECH_STATUS_ABGELEHNT


# ===================== /auftraege/{id}/fortschritt =====================

@pytest.mark.asyncio
async def test_fortschritt_invalid_id_400(monkeypatch):
    monkeypatch.setattr(app_screens, "get_session", lambda: _FakeObjSession(None))
    resp = await app_screens.api_auftrag_fortschritt(
        angebot_id="x", request=_req({"fortschritt": 50}), _e=None, _c=None)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_fortschritt_missing_value_400(monkeypatch):
    monkeypatch.setattr(app_screens, "get_session", lambda: _FakeObjSession(SimpleNamespace()))
    resp = await app_screens.api_auftrag_fortschritt(
        angebot_id=str(uuid.uuid4()), request=_req({}), _e=None, _c=None)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_fortschritt_not_found_404(monkeypatch):
    monkeypatch.setattr(app_screens, "get_session", lambda: _FakeObjSession(None))
    resp = await app_screens.api_auftrag_fortschritt(
        angebot_id=str(uuid.uuid4()), request=_req({"fortschritt": 50}), _e=None, _c=None)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_fortschritt_sets_value_clamped(monkeypatch):
    ang = SimpleNamespace(status=ANGEBOT_STATUS_WORK_IN_PROGRESS, arbeit_fortschritt=0)
    monkeypatch.setattr(app_screens, "get_session", lambda: _FakeObjSession(ang))
    resp = await app_screens.api_auftrag_fortschritt(
        angebot_id=str(uuid.uuid4()), request=_req({"fortschritt": 250}), _e=None, _c=None)
    j = _body(resp)
    assert ang.arbeit_fortschritt == 100   # geclamped
    assert j["fertig"] is True             # 100 % -> fertig
    assert ang.status == ANGEBOT_STATUS_WORK_DONE


@pytest.mark.asyncio
async def test_fortschritt_below_100_keeps_status(monkeypatch):
    ang = SimpleNamespace(status=ANGEBOT_STATUS_WORK_IN_PROGRESS, arbeit_fortschritt=0)
    monkeypatch.setattr(app_screens, "get_session", lambda: _FakeObjSession(ang))
    resp = await app_screens.api_auftrag_fortschritt(
        angebot_id=str(uuid.uuid4()), request=_req({"fortschritt": 60}), _e=None, _c=None)
    j = _body(resp)
    assert ang.arbeit_fortschritt == 60
    assert j["fertig"] is False
    assert ang.status == ANGEBOT_STATUS_WORK_IN_PROGRESS


# ===================== /rechnung/senden =====================

@pytest.mark.asyncio
async def test_rechnung_senden_feature_gated(monkeypatch):
    import core.features.check as fc

    async def _no(tid, key):
        return False
    monkeypatch.setattr(fc, "is_feature_enabled", _no)
    resp = await app_screens.api_q_rechnung_senden(
        request=_req({"angebot_id": str(uuid.uuid4())}), _e=None, _c=None)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_rechnung_senden_invalid_id_400(monkeypatch):
    import core.features.check as fc

    async def _yes(tid, key):
        return True
    monkeypatch.setattr(fc, "is_feature_enabled", _yes)
    resp = await app_screens.api_q_rechnung_senden(
        request=_req({"angebot_id": "kaputt"}), _e=None, _c=None)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_rechnung_senden_delegates_to_document_flow(monkeypatch):
    import core.features.check as fc
    import core.services.document_flow as dfl

    async def _yes(tid, key):
        return True
    monkeypatch.setattr(fc, "is_feature_enabled", _yes)

    captured = {}

    async def _fake_finalize(tid, *, angebot_id, anschreiben=None, kunde_email_override=None):
        captured["anschreiben"] = anschreiben
        captured["email"] = kunde_email_override
        return {"ok": True, "mail_sent": True, "email_used": "k@x.de",
                "status": "rechnung_gesendet", "kunde": "C"}
    monkeypatch.setattr(dfl, "finalize_and_send_invoice", _fake_finalize)

    resp = await app_screens.api_q_rechnung_senden(
        request=_req({"angebot_id": str(uuid.uuid4()),
                      "anschreiben": "Mein Text", "kunde_email": "k@x.de"}),
        _e=None, _c=None)
    assert resp.status_code == 200
    j = _body(resp)
    assert j["mail_sent"] is True
    assert captured["anschreiben"] == "Mein Text"
    assert captured["email"] == "k@x.de"
