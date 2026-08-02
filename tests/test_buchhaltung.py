"""Tests für die Buchhaltungs-Auswertung (core/services/buchhaltung.py)
und den zusammengeführten Bereich (GET /app/api/buchhaltung).

Reine Unit-Tests mit Fakes — keine echte DB (Muster wie test_app_aktuelles.py).
Die interessanten Fälle sind die Grenzen: Zahlungsziel exakt erreicht (noch
nicht überfällig), Entwurf ohne Versand (zählt NICHT zum offenen Geld) und
kaputte ToolConfig-Werte (dürfen die Übersicht nicht kippen).
"""
from __future__ import annotations

import datetime as dt
import json
import uuid
from types import SimpleNamespace

import pytest

from core.api import app_screens
from core.services import buchhaltung as buch


def _jetzt():
    return dt.datetime.now(dt.timezone.utc)


def _rechnung(*, betrag="100.00", status="mail_sent", tage_her=0,
              bezahlt_am=None, nummer="RE-1", invoice_id=None):
    raus = _jetzt() - dt.timedelta(days=tage_her)
    return SimpleNamespace(
        id=uuid.uuid4(),
        kunde_name="Müller",
        lexware_voucher_number=nummer,
        betrag_brutto_eur=betrag,
        status=status,
        mail_sent_at=raus if status == "mail_sent" else None,
        drafted_at=raus,
        created_at=raus,
        bezahlt_am=bezahlt_am,
        lexware_invoice_id=invoice_id,
    )


def _angebot(*, betrag="500.00", tage_her=0, quotation_id=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        kunde_name="Schmitz",
        gesamtbetrag_brutto_eur=betrag,
        created_at=_jetzt() - dt.timedelta(days=tage_her),
        lexware_quotation_id=quotation_id,
        status="mail_sent",
    )


class _FakeSession:
    """Gibt der Reihe nach die vorbereiteten Ergebnis-Listen zurück —
    uebersicht() setzt genau drei Queries ab (offen, bezahlt, Angebote)."""

    def __init__(self, *ergebnisse):
        self._ergebnisse = list(ergebnisse)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt):
        rows = self._ergebnisse.pop(0) if self._ergebnisse else []
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))


def _patch_session(monkeypatch, *ergebnisse):
    import core.database.connection as conn
    monkeypatch.setattr(conn, "get_session", lambda: _FakeSession(*ergebnisse))


async def _einstellungen_fest(_tid):
    return {"zahlungsziel_tage": 14, "nachfass_tage": 7}


# ===================== Kennzahlen / offene Posten =====================

@pytest.mark.asyncio
async def test_ueberfaellig_erst_nach_zahlungsziel(monkeypatch):
    monkeypatch.setattr(buch, "einstellungen", _einstellungen_fest)
    _patch_session(
        monkeypatch,
        [_rechnung(tage_her=14, betrag="100.00"),    # exakt am Ziel -> offen
         _rechnung(tage_her=15, betrag="200.00")],   # einen Tag drüber -> überfällig
        [], [],
    )
    d = await buch.uebersicht(uuid.uuid4())
    k = d["kennzahlen"]
    assert k["offen_anzahl"] == 2
    assert k["offen_eur"] == 300.0
    assert k["ueberfaellig_anzahl"] == 1
    assert k["ueberfaellig_eur"] == 200.0
    # Überfälliges steht oben
    assert d["offene_posten"][0]["ueberfaellig"] is True


@pytest.mark.asyncio
async def test_entwurf_zaehlt_nicht_als_offenes_geld(monkeypatch):
    monkeypatch.setattr(buch, "einstellungen", _einstellungen_fest)
    _patch_session(
        monkeypatch,
        [_rechnung(status="drafted", tage_her=30, betrag="900.00")],
        [], [],
    )
    d = await buch.uebersicht(uuid.uuid4())
    k = d["kennzahlen"]
    # Nicht versendet: der Kunde weiß von nichts, das ist kein Außenstand —
    # aber der Betrieb muss es sehen (entwuerfe_anzahl).
    assert k["offen_anzahl"] == 0
    assert k["offen_eur"] == 0.0
    assert k["ueberfaellig_anzahl"] == 0
    assert k["entwuerfe_anzahl"] == 1
    assert d["offene_posten"][0]["versendet"] is False


@pytest.mark.asyncio
async def test_bezahlt_30_tage_summiert(monkeypatch):
    monkeypatch.setattr(buch, "einstellungen", _einstellungen_fest)
    bezahlt = [_rechnung(betrag="120.50", bezahlt_am=_jetzt()),
               _rechnung(betrag="79.50", bezahlt_am=_jetzt())]
    _patch_session(monkeypatch, [], bezahlt, [])
    d = await buch.uebersicht(uuid.uuid4())
    assert d["kennzahlen"]["bezahlt_30t_eur"] == 200.0
    assert d["kennzahlen"]["bezahlt_30t_anzahl"] == 2


@pytest.mark.asyncio
async def test_nachfassen_erst_ab_frist(monkeypatch):
    monkeypatch.setattr(buch, "einstellungen", _einstellungen_fest)
    _patch_session(
        monkeypatch, [], [],
        [_angebot(tage_her=3), _angebot(tage_her=9), _angebot(tage_her=40)],
    )
    d = await buch.uebersicht(uuid.uuid4())
    # Angebote zählen alle zum offenen Volumen, nachfassen nur die alten
    assert d["kennzahlen"]["angebote_offen_anzahl"] == 3
    assert [n["tage"] for n in d["nachfassen"]] == [40, 9]


@pytest.mark.asyncio
async def test_lexware_deeplink_wird_gesetzt(monkeypatch):
    monkeypatch.setattr(buch, "einstellungen", _einstellungen_fest)
    inv = uuid.uuid4()
    _patch_session(monkeypatch, [_rechnung(invoice_id=inv)], [], [])
    d = await buch.uebersicht(uuid.uuid4())
    assert str(inv) in d["offene_posten"][0]["lexware_link"]


@pytest.mark.asyncio
async def test_ohne_lexware_id_kein_link(monkeypatch):
    monkeypatch.setattr(buch, "einstellungen", _einstellungen_fest)
    _patch_session(monkeypatch, [_rechnung(invoice_id=None)], [], [])
    d = await buch.uebersicht(uuid.uuid4())
    assert d["offene_posten"][0]["lexware_link"] is None


# ===================== Einstellungen =====================

@pytest.mark.asyncio
async def test_zahlungsziel_aus_toolconfig(monkeypatch):
    class _S:
        async def __aenter__(self): return self
        async def __aexit__(self, *e): return False
        async def execute(self, stmt):
            return SimpleNamespace(scalar_one_or_none=lambda: {"zahlungsziel_tage": 30})
    import core.database.connection as conn
    monkeypatch.setattr(conn, "get_session", lambda: _S())
    e = await buch.einstellungen(uuid.uuid4())
    assert e["zahlungsziel_tage"] == 30
    assert e["nachfass_tage"] == buch.NACHFASS_DEFAULT_TAGE


@pytest.mark.asyncio
async def test_unsinniges_zahlungsziel_faellt_auf_default(monkeypatch):
    class _S:
        async def __aenter__(self): return self
        async def __aexit__(self, *e): return False
        async def execute(self, stmt):
            return SimpleNamespace(scalar_one_or_none=lambda: {"zahlungsziel_tage": 9999})
    import core.database.connection as conn
    monkeypatch.setattr(conn, "get_session", lambda: _S())
    e = await buch.einstellungen(uuid.uuid4())
    assert e["zahlungsziel_tage"] == buch.ZAHLUNGSZIEL_DEFAULT_TAGE


@pytest.mark.asyncio
async def test_kaputte_toolconfig_kippt_nicht(monkeypatch):
    class _S:
        async def __aenter__(self): return self
        async def __aexit__(self, *e): return False
        async def execute(self, stmt):
            return SimpleNamespace(scalar_one_or_none=lambda: {"zahlungsziel_tage": "viele"})
    import core.database.connection as conn
    monkeypatch.setattr(conn, "get_session", lambda: _S())
    e = await buch.einstellungen(uuid.uuid4())
    assert e["zahlungsziel_tage"] == buch.ZAHLUNGSZIEL_DEFAULT_TAGE


# ===================== Text für Q =====================

def test_als_text_ohne_posten():
    assert "keine Rechnungen offen" in buch.als_text(
        {"kennzahlen": {}, "offene_posten": []})


def test_als_text_nennt_ueberfaellige():
    daten = {
        "zahlungsziel_tage": 14,
        "kennzahlen": {"offen_eur": 300.0, "offen_anzahl": 2,
                       "ueberfaellig_eur": 200.0, "ueberfaellig_anzahl": 1,
                       "entwuerfe_anzahl": 0},
        "offene_posten": [
            {"kunde": "Müller", "nummer": "RE-1", "betrag_eur": 200.0,
             "tage": 20, "versendet": True, "ueberfaellig": True},
        ],
    }
    text = buch.als_text(daten)
    assert "überfällig" in text
    assert "Müller" in text
    assert "200,00" in text


# ===================== GET /app/api/buchhaltung =====================

def _req(tenant_id=None):
    return SimpleNamespace(
        state=SimpleNamespace(app_tenant=SimpleNamespace(id=tenant_id or uuid.uuid4())),
        query_params={},
    )


def _body(resp):
    return json.loads(bytes(resp.body))


@pytest.mark.asyncio
async def test_endpoint_403_ohne_lexware(monkeypatch):
    import core.features.check as check
    async def _aus(tid, key):
        return False
    monkeypatch.setattr(check, "is_feature_enabled", _aus)
    resp = await app_screens.api_buchhaltung(request=_req(), _e=None)
    assert resp.status_code == 403
    assert _body(resp)["ok"] is False


@pytest.mark.asyncio
async def test_endpoint_buendelt_alle_listen(monkeypatch):
    import core.features.check as check
    async def _an(tid, key):
        return True
    monkeypatch.setattr(check, "is_feature_enabled", _an)

    async def _uebersicht(tid, **kw):
        return {"kennzahlen": {"offen_eur": 1.0}, "zahlungsziel_tage": 14,
                "offene_posten": [{"kunde": "A"}], "nachfassen": [], "nachfass_tage": 7}
    async def _ang(tid, limit=50):
        return [{"kunde": "B"}]
    async def _rech(tid, limit=50):
        return [{"kunde": "C"}]
    async def _bel(tid, limit=20):
        return [{"caption": "Quittung"}]
    monkeypatch.setattr(buch, "uebersicht", _uebersicht)
    monkeypatch.setattr(app_screens, "_angebote_liste", _ang)
    monkeypatch.setattr(app_screens, "_rechnungen_liste", _rech)
    monkeypatch.setattr(app_screens, "_recent_belege", _bel)

    j = _body(await app_screens.api_buchhaltung(request=_req(), _e=None))
    assert j["ok"] is True
    assert j["kennzahlen"]["offen_eur"] == 1.0
    assert j["offene_posten"][0]["kunde"] == "A"
    assert j["angebote"][0]["kunde"] == "B"
    assert j["rechnungen"][0]["kunde"] == "C"
    assert j["belege"][0]["caption"] == "Quittung"
