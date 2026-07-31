"""Tests fuer die Auftragsstunden (Buchung am Fortschrittsregler).

Schwerpunkte:
* Eingabe-Parsing — auf einer deutschen Handytastatur kommt „6,5" an,
  nicht „6.5"; verrutschte Kommastellen muessen abprallen.
* Loeschrechte — eigene Buchungen ja, fremde nur als Inhaber.
* Auswertung — Summe je Mitarbeiter, damit am Auftrag steht, wer wie
  lange dran war.

Reine Unit-Tests mit Fakes, keine echte DB (Muster wie test_app_auftraege).
"""
from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest

from core.api import app_screens
from core.services import auftrag_stunden as svc


def _req(body=None, tenant_id=None, is_inhaber=False):
    req = SimpleNamespace()

    async def _json():
        return body if body is not None else {}
    req.json = _json
    req.state = SimpleNamespace(
        app_tenant=SimpleNamespace(id=tenant_id or uuid.uuid4()),
        app_is_inhaber=is_inhaber)
    return req


def _body(resp):
    return json.loads(bytes(resp.body))


# =====================================================================
# Eingabe
# =====================================================================

def test_stunden_parsen_akzeptiert_komma_und_punkt():
    assert svc.parse_stunden("6,5") == Decimal("6.50")
    assert svc.parse_stunden("6.5") == Decimal("6.50")
    assert svc.parse_stunden("8") == Decimal("8.00")
    assert svc.parse_stunden(7.25) == Decimal("7.25")
    assert svc.parse_stunden(" 3 ") == Decimal("3.00")


def test_stunden_parsen_weist_unsinn_ab():
    """0 ist keine Buchung, 25 h an einem Tag ist ein Tippfehler."""
    assert svc.parse_stunden("0") is None
    assert svc.parse_stunden("-2") is None
    assert svc.parse_stunden("25") is None      # verrutschte Kommastelle
    assert svc.parse_stunden("acht") is None
    assert svc.parse_stunden("") is None
    assert svc.parse_stunden(None) is None


def test_stunden_formatieren_deutsch():
    assert svc.fmt_stunden(Decimal("6.50")) == "6,5 h"
    assert svc.fmt_stunden(Decimal("8.00")) == "8 h"
    assert svc.fmt_stunden(0) == "0 h"
    assert svc.fmt_stunden(None) == "0 h"


# =====================================================================
# Buchen
# =====================================================================

@pytest.mark.asyncio
async def test_buchung_braucht_gueltige_stunden(monkeypatch):
    async def _gibts(tid, aid):
        raise AssertionError("Auftrag darf gar nicht erst geladen werden")
    monkeypatch.setattr(app_screens, "_auftrag_fuer_stunden", _gibts)

    resp = await app_screens.api_auftrag_stunden_buchen(
        angebot_id=str(uuid.uuid4()), request=_req({"stunden": "25"}),
        emp=SimpleNamespace(id=uuid.uuid4(), name="Sven"), _c=None)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_buchung_laeuft_auf_den_angemeldeten_mitarbeiter(monkeypatch):
    """Niemand bucht Stunden auf jemand anderen — der Mitarbeiter kommt
    aus der Session, nicht aus dem Body."""
    aid = uuid.uuid4()
    emp = SimpleNamespace(id=uuid.uuid4(), name="Henrik")
    gesehen = {}

    async def _gibts(tid, a):
        return True
    monkeypatch.setattr(app_screens, "_auftrag_fuer_stunden", _gibts)

    async def _buche(tenant_id, angebot_id, **kw):
        gesehen.update(kw)
        gesehen["angebot_id"] = angebot_id
        return uuid.uuid4()
    monkeypatch.setattr(svc, "buche_stunden", _buche)

    async def _uebersicht(tid, a):
        return {"gesamt_text": "6,5 h", "je_mitarbeiter": [], "eintraege": []}
    monkeypatch.setattr(svc, "stunden_uebersicht", _uebersicht)

    resp = await app_screens.api_auftrag_stunden_buchen(
        angebot_id=str(aid),
        request=_req({"stunden": "6,5", "notiz": "Fliesen geschnitten",
                      "employee_id": str(uuid.uuid4())}),
        emp=emp, _c=None)
    assert resp.status_code == 200
    assert gesehen["employee_id"] == emp.id
    assert gesehen["employee_name"] == "Henrik"
    assert gesehen["stunden"] == Decimal("6.50")
    assert gesehen["angebot_id"] == aid


@pytest.mark.asyncio
async def test_buchung_verweigert_zukunft(monkeypatch):
    morgen = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    resp = await app_screens.api_auftrag_stunden_buchen(
        angebot_id=str(uuid.uuid4()),
        request=_req({"stunden": "4", "datum": morgen}),
        emp=SimpleNamespace(id=uuid.uuid4(), name="Sven"), _c=None)
    assert resp.status_code == 400
    assert "Zukunft" in _body(resp)["error"]


@pytest.mark.asyncio
async def test_buchung_auf_fremden_auftrag_ist_nicht_gefunden(monkeypatch):
    """Tenant-Isolation: ein Auftrag eines anderen Betriebs existiert
    fuer diesen Mitarbeiter schlicht nicht."""
    async def _gibts(tid, aid):
        return False
    monkeypatch.setattr(app_screens, "_auftrag_fuer_stunden", _gibts)

    async def _buche(*a, **kw):
        raise AssertionError("Es darf nichts gebucht werden")
    monkeypatch.setattr(svc, "buche_stunden", _buche)

    resp = await app_screens.api_auftrag_stunden_buchen(
        angebot_id=str(uuid.uuid4()), request=_req({"stunden": "4"}),
        emp=SimpleNamespace(id=uuid.uuid4(), name="Sven"), _c=None)
    assert resp.status_code == 404


# =====================================================================
# Loeschen
# =====================================================================

class _FakeSession:
    def __init__(self, obj):
        self.obj = obj
        self.geloescht = None
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: self.obj)

    async def delete(self, obj):
        self.geloescht = obj

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_eigene_buchung_darf_geloescht_werden(monkeypatch):
    emp_id, aid = uuid.uuid4(), uuid.uuid4()
    eintrag = SimpleNamespace(employee_id=emp_id, angebot_id=aid)
    sess = _FakeSession(eintrag)
    monkeypatch.setattr(svc, "get_session", lambda: sess)

    ok, fehler, gehoert_zu = await svc.loesche_stunden(
        uuid.uuid4(), uuid.uuid4(), employee_id=emp_id, darf_alles=False)
    assert ok is True and fehler == ""
    assert gehoert_zu == aid
    assert sess.geloescht is eintrag


@pytest.mark.asyncio
async def test_fremde_buchung_bleibt_stehen(monkeypatch):
    eintrag = SimpleNamespace(employee_id=uuid.uuid4(), angebot_id=uuid.uuid4())
    sess = _FakeSession(eintrag)
    monkeypatch.setattr(svc, "get_session", lambda: sess)

    ok, fehler, _ = await svc.loesche_stunden(
        uuid.uuid4(), uuid.uuid4(), employee_id=uuid.uuid4(), darf_alles=False)
    assert ok is False
    assert "deine" in fehler
    assert sess.geloescht is None


@pytest.mark.asyncio
async def test_inhaber_darf_fremde_buchung_loeschen(monkeypatch):
    eintrag = SimpleNamespace(employee_id=uuid.uuid4(), angebot_id=uuid.uuid4())
    sess = _FakeSession(eintrag)
    monkeypatch.setattr(svc, "get_session", lambda: sess)

    ok, _, _ = await svc.loesche_stunden(
        uuid.uuid4(), uuid.uuid4(), employee_id=uuid.uuid4(), darf_alles=True)
    assert ok is True
    assert sess.geloescht is eintrag


@pytest.mark.asyncio
async def test_loeschen_fremder_stunden_gibt_403(monkeypatch):
    async def _loesche(tid, eid, *, employee_id, darf_alles):
        return False, "Das sind nicht deine Stunden.", None
    monkeypatch.setattr(svc, "loesche_stunden", _loesche)

    resp = await app_screens.api_auftrag_stunden_loeschen(
        angebot_id=str(uuid.uuid4()), eintrag_id=str(uuid.uuid4()),
        request=_req(), emp=SimpleNamespace(id=uuid.uuid4()), _c=None)
    assert resp.status_code == 403


# =====================================================================
# Auswertung
# =====================================================================

@pytest.mark.asyncio
async def test_uebersicht_summiert_je_mitarbeiter(monkeypatch):
    """Das ist die Zeile, die am Auftrag stehen soll: wer wie lange."""
    sven, henrik = uuid.uuid4(), uuid.uuid4()
    heute = dt.date.today()
    eintraege = [
        SimpleNamespace(id=uuid.uuid4(), employee_id=sven, employee_name="Sven",
                        stunden=Decimal("6.50"), datum=heute, notiz="Fliesen"),
        SimpleNamespace(id=uuid.uuid4(), employee_id=henrik, employee_name="Henrik",
                        stunden=Decimal("2.00"), datum=heute, notiz=None),
        SimpleNamespace(id=uuid.uuid4(), employee_id=sven, employee_name="Sven",
                        stunden=Decimal("1.50"), datum=heute, notiz=None),
    ]

    class _Sess:
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False
        async def execute(self, stmt):
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: eintraege))
    monkeypatch.setattr(svc, "get_session", lambda: _Sess())

    u = await svc.stunden_uebersicht(uuid.uuid4(), uuid.uuid4())
    assert u["gesamt_text"] == "10 h"
    # Sortiert nach Stunden, damit oben steht, wer am meisten geleistet hat.
    assert [(x["name"], x["text"]) for x in u["je_mitarbeiter"]] == [
        ("Sven", "8 h"), ("Henrik", "2 h")]
    assert len(u["eintraege"]) == 3


@pytest.mark.asyncio
async def test_summen_ohne_auftraege_spart_die_abfrage(monkeypatch):
    def _darf_nicht():
        raise AssertionError("Ohne Auftraege darf keine Abfrage laufen")
    monkeypatch.setattr(svc, "get_session", _darf_nicht)
    assert await svc.summen_je_auftrag(uuid.uuid4(), []) == {}


@pytest.mark.asyncio
async def test_listen_bekommen_stunden_nur_fuer_laufende(monkeypatch):
    """Abgerechnete Auftraege sollen nicht mit Zahlen zuwachsen — und
    es soll EINE Sammelabfrage sein, nicht eine pro Karte."""
    laufend, fertig, alt = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    zeilen = [
        {"id": laufend, "in_arbeit": True, "fertig": False},
        {"id": fertig, "in_arbeit": False, "fertig": True},
        {"id": alt, "in_arbeit": False, "fertig": False},
    ]
    aufrufe = []

    async def _summen(tid, ids):
        aufrufe.append(ids)
        return {laufend: {"gesamt": 8.0, "gesamt_text": "8 h",
                          "text": "Sven 8 h"}}
    monkeypatch.setattr(svc, "summen_je_auftrag", _summen)

    await app_screens._stunden_anreichern(uuid.uuid4(), zeilen)
    assert len(aufrufe) == 1
    assert len(aufrufe[0]) == 2                    # laufend + fertig, nicht alt
    assert zeilen[0]["stunden_text"] == "Sven 8 h"
    assert zeilen[1]["stunden_text"] == ""         # noch nichts gebucht
    assert "stunden_text" not in zeilen[2]         # gar nicht erst gefragt
