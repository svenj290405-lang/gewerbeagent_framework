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


@pytest.fixture(autouse=True)
def _keine_stundenabfrage(monkeypatch):
    """Die Auftragsliste reichert seit den Auftragsstunden noch eine Summe
    an. Die laeuft ueber eine EIGENE Session im Stunden-Service, die
    ``app_screens.get_session`` nicht mitfaelscht — hier stillgelegt, damit
    die Tests dieser Datei ohne echte DB bleiben (eigene Abdeckung in
    test_auftrag_stunden.py)."""
    import core.services.auftrag_stunden as stunden_svc

    async def _keine(tid, ids):
        return {}
    monkeypatch.setattr(stunden_svc, "summen_je_auftrag", _keine)


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


def _req(body=None, tenant_id=None, permissions=None, emp_id=None):
    """Fake-Request.

    app_employee/app_permissions setzt im Echtbetrieb require_app_user;
    der Auftrags-Scope (core/security/app_scope.py) liest beides. Default
    hier: darf alles sehen — die Sichtbarkeitsgrenze hat ihre eigenen
    Tests in test_auftraege_scope.py.
    """
    req = SimpleNamespace()

    async def _json():
        return body or {}
    req.json = _json
    if permissions is None:
        permissions = {"auftraege.alle_sehen", "auftraege.fuehren"}
    req.state = SimpleNamespace(
        app_tenant=SimpleNamespace(id=tenant_id or uuid.uuid4()),
        app_employee=SimpleNamespace(id=emp_id or uuid.uuid4(), slug="sven"),
        app_permissions=frozenset(permissions),
    )
    return req


def _json_body(resp):
    return json.loads(bytes(resp.body))


# =====================================================================
# GET /auftraege
# =====================================================================

def _ang(**kw):
    """Angebot-Attrappe mit allen Feldern, die _auftrag_zeile liest."""
    basis = dict(
        id=uuid.uuid4(), kunde_name="Mueller", gesamtbetrag_brutto_eur=1000,
        status="accepted", created_at=None, updated_at=None,
        arbeit_fortschritt=0,
        abgeschlossen_am=None, archiv_drive_folder_url=None,
        assigned_employee_id=None,
    )
    basis.update(kw)
    return SimpleNamespace(**basis)


@pytest.mark.asyncio
async def test_auftraege_list_maps_lifecycle(monkeypatch):
    rows = [
        _ang(kunde_name="Mueller", status="accepted"),
        _ang(kunde_name="Schmidt", gesamtbetrag_brutto_eur=None, status="abgebrochen"),
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


@pytest.mark.asyncio
async def test_laufende_liste_zeigt_regler_nur_bei_arbeit_laeuft(monkeypatch):
    """Der Fortschritts-Regler gehoert an genau EINEN Schritt: 'Arbeit
    laeuft'. Die Liste reicht das per in_arbeit-Flag durch."""
    rows = [
        _ang(kunde_name="Arbeitet", status="arbeit_laeuft", arbeit_fortschritt=60),
        _ang(kunde_name="Wartet", status="accepted"),
    ]
    monkeypatch.setattr(app_screens, "get_session", lambda: _FakeListSession(rows))
    j = _json_body(await app_screens.api_auftraege(request=_req(), _e=None))
    arbeitet, wartet = j["auftraege"]
    assert arbeitet["in_arbeit"] is True and arbeitet["fortschritt"] == 60
    assert wartet["in_arbeit"] is False


@pytest.mark.asyncio
async def test_abgeschlossene_liste_liefert_archiv_link(monkeypatch):
    rows = [_ang(
        kunde_name="Fertig", status="rechnung_gesendet",
        archiv_drive_folder_url="https://drive.google.com/drive/folders/abc",
    )]
    monkeypatch.setattr(app_screens, "get_session", lambda: _FakeListSession(rows))
    j = _json_body(await app_screens.api_auftraege_abgeschlossen(request=_req(), _e=None))
    assert len(j["auftraege"]) == 1
    assert j["auftraege"][0]["archiv_url"].endswith("/abc")


def test_laufende_liste_schliesst_fertiggestellte_aus():
    """Fertiggestellte Auftraege gehoeren in die Historie, nicht in die
    Arbeitsliste — sonst waechst die ewig. Das gilt fuer abgerechnete
    (rechnung_gesendet) UND fuer abgebrochene: an beiden ist nichts mehr
    zu tun."""
    from core.models.angebot import (
        ANGEBOT_STATUS_ABGEBROCHEN, ANGEBOT_STATUS_RECHNUNG_GESENDET,
        AUFTRAG_LIFECYCLE,
    )
    laufend = set(AUFTRAG_LIFECYCLE) - {ANGEBOT_STATUS_RECHNUNG_GESENDET}
    assert ANGEBOT_STATUS_RECHNUNG_GESENDET not in laufend
    assert ANGEBOT_STATUS_ABGEBROCHEN not in laufend
    assert "arbeit_laeuft" in laufend
    # ... und genau die beiden bilden die Historie.
    assert app_screens._AUFTRAG_HISTORIE_STATES == {
        ANGEBOT_STATUS_RECHNUNG_GESENDET, ANGEBOT_STATUS_ABGEBROCHEN}


@pytest.mark.asyncio
async def test_historie_enthaelt_abgerechnete_und_abgebrochene(monkeypatch):
    """Die Historie ist die vollstaendige Vergangenheit: beide Endzustaende
    stehen drin, jeder mit dem Zeitpunkt, an dem er durch war."""
    import datetime as _dt

    abbruch = _dt.datetime(2026, 3, 4, 9, 30)
    rows = [
        _ang(kunde_name="Fertig", status="rechnung_gesendet",
             abgeschlossen_am=_dt.datetime(2026, 5, 1, 12, 0)),
        _ang(kunde_name="Geplatzt", status="abgebrochen", updated_at=abbruch),
    ]
    monkeypatch.setattr(app_screens, "get_session", lambda: _FakeListSession(rows))
    j = _json_body(await app_screens.api_auftraege_historie(request=_req(), _e=None))
    fertig, geplatzt = j["auftraege"]
    assert fertig["abgebrochen"] is False and fertig["beendet_am"]
    # Abgebrochene haben keinen eigenen Stempel — updated_at ist der Abbruch.
    assert geplatzt["abgebrochen"] is True
    assert geplatzt["beendet_am"] == app_screens._fmt_dt(abbruch)


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


@pytest.mark.asyncio
async def test_abgerechneter_auftrag_bleibt_abgerechnet(monkeypatch):
    """Audit 2026-08-24: geprueft wurde nur der ZIEL-Status. Ein abgerechneter
    Auftrag liess sich auf "fertig" zuruecksetzen — danach stand der Knopf
    "Rechnung stellen" wieder da, ein Klick = zweite Rechnungsnummer."""
    ang = SimpleNamespace(status="rechnung_gesendet", accepted_at=None)
    sess = _FakeObjSession(ang)
    monkeypatch.setattr(app_screens, "get_session", lambda: sess)
    resp = await app_screens.api_auftrag_status(
        angebot_id=str(uuid.uuid4()),
        request=_req({"status": "arbeit_fertig"}), _e=None, _c=None,
    )
    assert resp.status_code == 400
    assert ang.status == "rechnung_gesendet"
    assert sess.committed is False


@pytest.mark.asyncio
async def test_abgebrochener_auftrag_bleibt_reaktivierbar(monkeypatch):
    """Ein Fehlklick auf "Abbrechen" muss sich reparieren lassen — nur der
    Geld-Pfad ist endgueltig."""
    ang = SimpleNamespace(status="abgebrochen", accepted_at=None)
    monkeypatch.setattr(app_screens, "get_session", lambda: _FakeObjSession(ang))
    resp = await app_screens.api_auftrag_status(
        angebot_id=str(uuid.uuid4()),
        request=_req({"status": "arbeit_laeuft"}), _e=None, _c=None,
    )
    assert resp.status_code == 200
    assert ang.status == "arbeit_laeuft"


# =====================================================================
# POST /auftraege/neu — Auftrag von Hand
# =====================================================================

def _fake_create(aufruf: dict):
    """Ersetzt create_auftrag_manuell und merkt sich die Argumente."""
    async def _create(tid, **kw):
        aufruf["tid"] = tid
        aufruf.update(kw)
        return {"ok": True, "id": "neue-id", "kunde": kw.get("kunde_name"),
                "status": kw.get("status") or "accepted"}
    return _create


@pytest.mark.asyncio
async def test_auftrag_neu_reicht_felder_durch(monkeypatch):
    aufruf: dict = {}
    from core.services import document_flow
    monkeypatch.setattr(document_flow, "create_auftrag_manuell", _fake_create(aufruf))
    tid = uuid.uuid4()
    resp = await app_screens.api_auftrag_neu(
        request=_req({
            "kunde_name": "Bauer", "kunde_email": "bauer@example.de",
            "kunde_strasse": "Hauptstr. 3", "kunde_plz": "54497", "kunde_ort": "Horath",
            "status": "arbeit_laeuft",
            "positionen": [{"name": "Bad fliesen", "menge": 1, "preis_brutto_eur": 900}],
        }, tenant_id=tid),
        _e=None, _c=None,
    )
    assert resp.status_code == 200
    assert _json_body(resp)["id"] == "neue-id"
    assert aufruf["tid"] == tid
    assert aufruf["kunde_name"] == "Bauer"
    assert aufruf["status"] == "arbeit_laeuft"
    assert aufruf["positionen"][0]["name"] == "Bad fliesen"


@pytest.mark.asyncio
async def test_auftrag_neu_begrenzt_positionen(monkeypatch):
    """Ein kaputter Client soll ueber das Formular keine tausend Zeilen
    anlegen koennen."""
    from core.services import document_flow
    monkeypatch.setattr(document_flow, "create_auftrag_manuell", _fake_create({}))
    viele = [{"name": f"P{i}", "preis_brutto_eur": 1} for i in range(51)]
    resp = await app_screens.api_auftrag_neu(
        request=_req({"kunde_name": "X", "positionen": viele}), _e=None, _c=None)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_auftrag_neu_lehnt_kaputte_positionen_ab(monkeypatch):
    from core.services import document_flow
    monkeypatch.setattr(document_flow, "create_auftrag_manuell", _fake_create({}))
    for positionen in ("keine-liste", ["nur ein String"]):
        resp = await app_screens.api_auftrag_neu(
            request=_req({"kunde_name": "X", "positionen": positionen}), _e=None, _c=None)
        assert resp.status_code == 400


@pytest.mark.asyncio
async def test_auftrag_neu_gibt_fachfehler_als_400_zurueck(monkeypatch):
    from core.services import document_flow

    async def _create(tid, **kw):
        return {"ok": False, "error": "Kundenname ist Pflicht."}
    monkeypatch.setattr(document_flow, "create_auftrag_manuell", _create)
    resp = await app_screens.api_auftrag_neu(
        request=_req({"kunde_name": "", "positionen": [{"name": "A"}]}), _e=None, _c=None)
    assert resp.status_code == 400
    assert "Pflicht" in _json_body(resp)["error"]
