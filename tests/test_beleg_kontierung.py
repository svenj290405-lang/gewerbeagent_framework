"""Tests für die Beleg-Vorkontierung (core/services/beleg_kontierung.py).

Der heikle Teil ist nicht die Extraktion (die macht Gemini), sondern was
danach passiert: Was wird nach Lexware geschrieben, und was wird bewusst
NICHT geschrieben. Ein halb ausgefüllter Beleg ist schlimmer als ein leerer,
weil er fertig aussieht.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from core.services import beleg_kontierung as bk


KATEGORIEN = [
    {"id": "cat-mat", "name": "Material/Waren", "type": "outgo"},
    {"id": "cat-kfz", "name": "Fahrzeugkosten", "type": "outgo"},
    {"id": "cat-erl", "name": "Erlöse", "type": "income"},
]


class _FakeProvider:
    def __init__(self, *, fehler=None):
        self.geschrieben = []
        self._fehler = fehler

    async def get_posting_categories(self):
        return KATEGORIEN

    async def update_voucher(self, voucher_id, changes):
        if self._fehler:
            raise self._fehler
        self.geschrieben.append((voucher_id, changes))
        return {"id": str(voucher_id)}


def _beleg(*, voucher_id=None, file_data=b"\xff\xd8bytes"):
    return SimpleNamespace(
        id=uuid.uuid4(), tenant_id=uuid.uuid4(),
        file_data=file_data, file_mime="image/jpeg",
        lexware_voucher_id=voucher_id,
    )


def _patch(monkeypatch, *, beleg, provider):
    class _S:
        async def __aenter__(self): return self
        async def __aexit__(self, *e): return False
        async def execute(self, stmt):
            return SimpleNamespace(scalar_one_or_none=lambda: beleg)
    import core.database.connection as conn
    import core.integrations.rechnung_payment_monitor as pm
    monkeypatch.setattr(conn, "get_session", lambda: _S())

    async def _build(tid):
        return provider
    monkeypatch.setattr(pm, "_build_lexware_provider", _build)
    bk.invalidate_kategorie_cache()


# ===================== uebernehmen: was wird geschrieben =====================

@pytest.mark.asyncio
async def test_uebernehmen_rechnet_steuer_aus_brutto(monkeypatch):
    prov = _FakeProvider()
    vid = uuid.uuid4()
    _patch(monkeypatch, beleg=_beleg(voucher_id=vid), provider=prov)
    r = await bk.uebernehmen(
        uuid.uuid4(), uuid.uuid4(),
        haendler="Bauhaus", datum="2026-07-30",
        betrag_brutto_eur=119.0, mwst_prozent=19, kategorie="Material/Waren")
    assert r["ok"] is True
    _, changes = prov.geschrieben[0]
    assert changes["type"] == "purchaseinvoice"
    assert changes["totalGrossAmount"] == 119.0
    assert changes["totalTaxAmount"] == 19.0          # 119 brutto bei 19 %
    assert changes["voucherItems"][0]["categoryId"] == "cat-mat"
    assert changes["voucherDate"].startswith("2026-07-30")
    assert changes["remark"] == "Bauhaus"


@pytest.mark.asyncio
async def test_uebernehmen_ohne_mwst_rechnet_keine_steuer(monkeypatch):
    prov = _FakeProvider()
    _patch(monkeypatch, beleg=_beleg(voucher_id=uuid.uuid4()), provider=prov)
    await bk.uebernehmen(
        uuid.uuid4(), uuid.uuid4(), datum="2026-07-30",
        betrag_brutto_eur=100.0, mwst_prozent=0, kategorie=None)
    _, changes = prov.geschrieben[0]
    assert changes["totalTaxAmount"] == 0.0
    assert "categoryId" not in changes["voucherItems"][0]


@pytest.mark.asyncio
async def test_ohne_betrag_wird_nichts_geschrieben(monkeypatch):
    prov = _FakeProvider()
    _patch(monkeypatch, beleg=_beleg(voucher_id=uuid.uuid4()), provider=prov)
    r = await bk.uebernehmen(uuid.uuid4(), uuid.uuid4(),
                             datum="2026-07-30", betrag_brutto_eur=None)
    assert r["ok"] is False
    assert prov.geschrieben == []


@pytest.mark.asyncio
async def test_ohne_datum_wird_nichts_geschrieben(monkeypatch):
    prov = _FakeProvider()
    _patch(monkeypatch, beleg=_beleg(voucher_id=uuid.uuid4()), provider=prov)
    r = await bk.uebernehmen(uuid.uuid4(), uuid.uuid4(),
                             datum=None, betrag_brutto_eur=50.0)
    assert r["ok"] is False
    assert prov.geschrieben == []


@pytest.mark.asyncio
async def test_erfundene_kategorie_wird_abgelehnt(monkeypatch):
    prov = _FakeProvider()
    _patch(monkeypatch, beleg=_beleg(voucher_id=uuid.uuid4()), provider=prov)
    r = await bk.uebernehmen(
        uuid.uuid4(), uuid.uuid4(), datum="2026-07-30",
        betrag_brutto_eur=50.0, kategorie="Kaffeekasse")
    assert r["ok"] is False
    assert prov.geschrieben == []


@pytest.mark.asyncio
async def test_beleg_ohne_lexware_id_wird_nicht_gebucht(monkeypatch):
    prov = _FakeProvider()
    _patch(monkeypatch, beleg=_beleg(voucher_id=None), provider=prov)
    r = await bk.uebernehmen(uuid.uuid4(), uuid.uuid4(),
                             datum="2026-07-30", betrag_brutto_eur=50.0)
    assert r["ok"] is False
    assert prov.geschrieben == []


@pytest.mark.asyncio
async def test_lexware_fehler_faellt_weich(monkeypatch):
    prov = _FakeProvider(fehler=RuntimeError("HTTP 400"))
    _patch(monkeypatch, beleg=_beleg(voucher_id=uuid.uuid4()), provider=prov)
    r = await bk.uebernehmen(uuid.uuid4(), uuid.uuid4(),
                             datum="2026-07-30", betrag_brutto_eur=50.0)
    assert r["ok"] is False
    # Der Nutzer soll wissen, dass der Beleg trotzdem drin liegt.
    assert "von Hand" in r["error"]


# ===================== vorschlag =====================

@pytest.mark.asyncio
async def test_vorschlag_gibt_gemini_nur_ausgabe_kategorien(monkeypatch):
    prov = _FakeProvider()
    _patch(monkeypatch, beleg=_beleg(voucher_id=uuid.uuid4()), provider=prov)
    gesehen = {}

    async def _fake_extract(daten, mime, *, kategorien=None, branche=None):
        gesehen["kategorien"] = kategorien
        return {"ist_beleg": True, "sicherheit": "hoch", "haendler": "Bauhaus",
                "datum": "2026-07-30", "betrag_brutto_eur": 119.0,
                "mwst_prozent": 19, "kategorie": "Material/Waren"}
    import core.ai.gemini as gem
    monkeypatch.setattr(gem, "extract_beleg_from_image", _fake_extract)

    r = await bk.vorschlag(uuid.uuid4(), uuid.uuid4())
    assert r["ok"] is True and r["ist_beleg"] is True
    # "Erlöse" ist eine Einnahme-Kategorie und darf gar nicht erst auftauchen
    assert "Erlöse" not in gesehen["kategorien"]
    assert "Material/Waren" in gesehen["kategorien"]
    assert r["kontierbar"] is True


@pytest.mark.asyncio
async def test_vorschlag_erkennt_wenn_es_kein_beleg_ist(monkeypatch):
    _patch(monkeypatch, beleg=_beleg(voucher_id=uuid.uuid4()), provider=_FakeProvider())

    async def _fake_extract(*a, **kw):
        return {"ist_beleg": False, "sicherheit": "niedrig"}
    import core.ai.gemini as gem
    monkeypatch.setattr(gem, "extract_beleg_from_image", _fake_extract)

    r = await bk.vorschlag(uuid.uuid4(), uuid.uuid4())
    assert r["ok"] is True
    assert r["ist_beleg"] is False


@pytest.mark.asyncio
async def test_vorschlag_ohne_datei(monkeypatch):
    _patch(monkeypatch, beleg=_beleg(file_data=None), provider=_FakeProvider())
    r = await bk.vorschlag(uuid.uuid4(), uuid.uuid4())
    assert r["ok"] is False


# ===================== Kategorie-Auswahl fuer den Prompt =====================

def test_bevorzugte_kategorien_stehen_vorne():
    kats = [{"name": "Zzz Sonstiges"}, {"name": "Material/Waren"},
            {"name": "Aaa Anderes"}, {"name": "Fahrzeugkosten"}]
    auswahl = bk._auswahl_fuer_gemini(kats, 3)
    assert auswahl[:2] == ["Material/Waren", "Fahrzeugkosten"]
    assert len(auswahl) == 3
