"""Tests fuer die Wissens-Endpunkte der PWA (core/api/app_screens.py).

Reine Unit-Tests mit Fakes, kein echter DB-Zugriff (Muster wie
tests/test_app_aktuelles.py). Geprueft wird vor allem, was schiefgehen
kann, wenn jemand die Validierung anfasst: unbekannte Kategorien,
unbekannte Sichtbarkeiten und fremde Tenant-IDs.
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from core.api import app_screens
from core.models.tenant_knowledge import SICHTBARKEIT_INTERN, SICHTBARKEIT_KUNDE


class _FakeSession:
    """Session-Fake: liefert ein Objekt zurueck und merkt sich Adds/Deletes."""

    def __init__(self, obj=None):
        self.obj = obj
        self.added = []
        self.deleted = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: self.obj)

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)

    async def flush(self):
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()

    async def commit(self):
        pass


def _req(body=None, tenant_id=None):
    req = SimpleNamespace()

    async def _json():
        return body or {}

    req.json = _json
    req.state = SimpleNamespace(
        app_tenant=SimpleNamespace(id=tenant_id or uuid.uuid4()),
        app_employee=SimpleNamespace(id=uuid.uuid4(), slug="sven"),
        app_permissions=frozenset({"wissen.pflegen"}),
    )
    req.query_params = {}
    return req


def _body(response):
    return json.loads(bytes(response.body).decode())


# ---------------------------------------------------------------------
# Anlegen
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wissen_anlegen_setzt_herkunft_und_bestaetigung(monkeypatch):
    sess = _FakeSession()
    monkeypatch.setattr(app_screens, "get_session", lambda: sess)

    tid = uuid.uuid4()
    res = await app_screens.api_wissen_add(
        _req({"kategorie": "preise", "text": "Stundensatz 75 EUR netto."}, tid)
    )
    assert _body(res)["ok"] is True
    eintrag = sess.added[0]
    assert eintrag.tenant_id == tid
    assert eintrag.quelle == "mensch"
    assert eintrag.sichtbarkeit == SICHTBARKEIT_KUNDE
    # Ohne diesen Stempel wuerde der Frische-Ping neue Eintraege sofort
    # als "seit ewig unbestaetigt" anmahnen.
    assert eintrag.zuletzt_bestaetigt_am is not None


@pytest.mark.asyncio
async def test_wissen_anlegen_kann_intern(monkeypatch):
    sess = _FakeSession()
    monkeypatch.setattr(app_screens, "get_session", lambda: sess)
    await app_screens.api_wissen_add(
        _req({"kategorie": "preise", "text": "EK Viessmann 40% Rabatt.",
              "sichtbarkeit": "intern"})
    )
    assert sess.added[0].sichtbarkeit == SICHTBARKEIT_INTERN


@pytest.mark.asyncio
async def test_wissen_anlegen_lehnt_unbekannte_kategorie_ab(monkeypatch):
    sess = _FakeSession()
    monkeypatch.setattr(app_screens, "get_session", lambda: sess)
    res = await app_screens.api_wissen_add(
        _req({"kategorie": "erfunden", "text": "irgendwas"})
    )
    assert res.status_code == 400
    assert sess.added == []


@pytest.mark.asyncio
async def test_unbekannte_sichtbarkeit_faellt_auf_kunde_zurueck(monkeypatch):
    """Fail-safe in die sichere Richtung waere 'intern' — hier ist 'kunde'
    richtig, weil der Wert aus der eigenen Oberflaeche kommt und ein
    stiller Wechsel auf 'intern' den Eintrag unbemerkt unwirksam machen
    wuerde. Wichtig ist, dass kein unbekannter Wert in die DB kommt."""
    sess = _FakeSession()
    monkeypatch.setattr(app_screens, "get_session", lambda: sess)
    await app_screens.api_wissen_add(
        _req({"kategorie": "faq", "text": "Wir arbeiten samstags.",
              "sichtbarkeit": "quatsch"})
    )
    assert sess.added[0].sichtbarkeit == SICHTBARKEIT_KUNDE


@pytest.mark.asyncio
async def test_anlegen_meldet_personenbezug_zurueck(monkeypatch):
    sess = _FakeSession()
    monkeypatch.setattr(app_screens, "get_session", lambda: sess)
    res = await app_screens.api_wissen_add(
        _req({"kategorie": "faq", "text": "Bei Fragen: max.mueller@example.de"})
    )
    daten = _body(res)
    # Gespeichert WIRD er trotzdem — der Hinweis informiert, blockiert nicht.
    assert daten["ok"] is True and sess.added
    assert daten["hinweis"] and "E-Mail" in daten["hinweis"]


# ---------------------------------------------------------------------
# Aendern
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_aendern_aktualisiert_und_bestaetigt(monkeypatch):
    eintrag = SimpleNamespace(
        id=uuid.uuid4(), text="alt", kategorie="preise",
        sichtbarkeit=SICHTBARKEIT_KUNDE, aktiv=True, zuletzt_bestaetigt_am=None,
    )
    sess = _FakeSession(eintrag)
    monkeypatch.setattr(app_screens, "get_session", lambda: sess)

    res = await app_screens.api_wissen_update(
        str(eintrag.id),
        _req({"text": "Stundensatz jetzt 80 EUR.", "aktiv": False,
              "sichtbarkeit": "intern"}),
    )
    assert _body(res)["ok"] is True
    assert eintrag.text == "Stundensatz jetzt 80 EUR."
    assert eintrag.aktiv is False
    assert eintrag.sichtbarkeit == SICHTBARKEIT_INTERN
    # Eine Aenderung ist zugleich eine Bestaetigung.
    assert eintrag.zuletzt_bestaetigt_am is not None


@pytest.mark.asyncio
async def test_aendern_lehnt_unbekannte_sichtbarkeit_ab(monkeypatch):
    eintrag = SimpleNamespace(
        id=uuid.uuid4(), text="alt", kategorie="preise",
        sichtbarkeit=SICHTBARKEIT_KUNDE, aktiv=True, zuletzt_bestaetigt_am=None,
    )
    monkeypatch.setattr(app_screens, "get_session", lambda: _FakeSession(eintrag))
    res = await app_screens.api_wissen_update(
        str(eintrag.id), _req({"sichtbarkeit": "oeffentlich"})
    )
    assert res.status_code == 400
    assert eintrag.sichtbarkeit == SICHTBARKEIT_KUNDE


@pytest.mark.asyncio
async def test_aendern_mit_kaputter_id_gibt_400(monkeypatch):
    monkeypatch.setattr(app_screens, "get_session", lambda: _FakeSession(None))
    res = await app_screens.api_wissen_update(
        "keine-uuid", _req({"text": "ein gueltiger Text"})
    )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_aendern_eines_fremden_eintrags_gibt_404(monkeypatch):
    """Die Query filtert auf tenant_id — ein fremder Eintrag kommt gar nicht
    erst zurueck, und der Endpunkt darf daraus keinen 500 machen.

    Der Text muss hier gueltig sein: seit dem Fix wird die Eingabe VOR dem
    DB-Zugriff geprueft, ein zu kurzer Text kaeme also mit 400 zurueck,
    bevor die Tenant-Isolation ueberhaupt greift."""
    monkeypatch.setattr(app_screens, "get_session", lambda: _FakeSession(None))
    res = await app_screens.api_wissen_update(
        str(uuid.uuid4()), _req({"text": "ein gueltiger Text"})
    )
    assert res.status_code == 404


# ---------------------------------------------------------------------
# Wissensluecken
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_luecke_beantworten_legt_eintrag_an_und_verknuepft(monkeypatch):
    from core.models.wissensluecke import STATUS_BEANTWORTET

    luecke = SimpleNamespace(
        id=uuid.uuid4(), status="offen", erledigt_am=None, knowledge_id=None,
    )
    sess = _FakeSession(luecke)
    monkeypatch.setattr(app_screens, "get_session", lambda: sess)

    res = await app_screens.api_wissensluecke_beantworten(
        str(luecke.id),
        _req({"text": "Ja, wir verlegen auch Vinyl.", "kategorie": "leistungen"}),
    )
    assert _body(res)["ok"] is True
    assert luecke.status == STATUS_BEANTWORTET
    assert luecke.erledigt_am is not None
    # Die Verknuepfung ist der Beleg, woher der Eintrag kam.
    assert luecke.knowledge_id == sess.added[0].id


@pytest.mark.asyncio
async def test_luecke_beantworten_braucht_text(monkeypatch):
    luecke = SimpleNamespace(id=uuid.uuid4(), status="offen")
    sess = _FakeSession(luecke)
    monkeypatch.setattr(app_screens, "get_session", lambda: sess)
    res = await app_screens.api_wissensluecke_beantworten(
        str(luecke.id), _req({"text": "ok"})
    )
    assert res.status_code == 400
    assert luecke.status == "offen"


# ---------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_import_uebernimmt_nur_gueltige_vorschlaege(monkeypatch):
    sess = _FakeSession()
    monkeypatch.setattr(app_screens, "get_session", lambda: sess)

    res = await app_screens.api_wissen_import_uebernehmen(_req({"eintraege": [
        {"kategorie": "leistungen", "text": "Wir bauen Baeder."},
        {"kategorie": "erfunden", "text": "wird verworfen"},
        {"kategorie": "preise", "text": "zu"},          # zu kurz
        "kein dict",
    ]}))
    assert _body(res)["uebernommen"] == 1
    assert len(sess.added) == 1
    # Importiertes ist als solches erkennbar — es kam nicht vom Menschen.
    assert sess.added[0].quelle == "import"


@pytest.mark.asyncio
async def test_import_ohne_eintraege_gibt_400(monkeypatch):
    monkeypatch.setattr(app_screens, "get_session", lambda: _FakeSession())
    res = await app_screens.api_wissen_import_uebernehmen(_req({"eintraege": []}))
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_ungueltige_kategorie_aendert_den_text_nicht(monkeypatch):
    """Regression: die Validierung lief frueher NACH dem Zuweisen. Eine
    ungueltige Kategorie hat dann die Textaenderung mit committet und dem
    Nutzer trotzdem einen Fehler gemeldet — er haette es nie bemerkt."""
    eintrag = SimpleNamespace(
        id=uuid.uuid4(), text="Stundensatz 75 EUR.", kategorie="preise",
        sichtbarkeit=SICHTBARKEIT_KUNDE, aktiv=True, zuletzt_bestaetigt_am=None,
    )
    monkeypatch.setattr(app_screens, "get_session", lambda: _FakeSession(eintrag))
    res = await app_screens.api_wissen_update(
        str(eintrag.id),
        _req({"text": "Stundensatz 999 EUR.", "kategorie": "erfunden"}),
    )
    assert res.status_code == 400
    assert eintrag.text == "Stundensatz 75 EUR."
    assert eintrag.zuletzt_bestaetigt_am is None
