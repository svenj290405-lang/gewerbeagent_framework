"""Tests für die Objekt-Erkennung mit Web-Erdung.

Der Gemini-Aufruf selbst wird gefakt — geprüft wird das Drumherum:
Quellen-Aufbereitung, Feature-Gate, MIME-/Größen-Grenzen und dass ein
gefundenes Teil sauber als Material landet (eindeutiger Slug!).
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from core.ai import gemini
from core.api import app_screens
from core.features.catalog import FEATURES


# ===================== Quellen aus den Grounding-Daten =====================

def _chunk(titel, domain, uri):
    return SimpleNamespace(web=SimpleNamespace(title=titel, domain=domain, uri=uri))


def _candidate(chunks, anfragen=None):
    return SimpleNamespace(grounding_metadata=SimpleNamespace(
        grounding_chunks=chunks, web_search_queries=anfragen or []))


def test_quellen_werden_pro_domain_entdoppelt():
    cand = _candidate([
        _chunk("Bosch Handbuch", "bosch-home.com", "https://x/1"),
        _chunk("Bosch FAQ", "bosch-home.com", "https://x/2"),
        _chunk("iFixit", "ifixit.com", "https://x/3"),
    ], ["bosch e18"])
    quellen, anfragen = gemini._grounding_quellen(cand)
    assert [q["domain"] for q in quellen] == ["bosch-home.com", "ifixit.com"]
    assert anfragen == ["bosch e18"]


def test_quellen_werden_gedeckelt():
    cand = _candidate([_chunk(f"T{i}", f"d{i}.de", f"https://x/{i}") for i in range(20)])
    quellen, _ = gemini._grounding_quellen(cand)
    assert len(quellen) == gemini.OBJEKT_MAX_QUELLEN


def test_ohne_grounding_keine_quellen():
    quellen, anfragen = gemini._grounding_quellen(SimpleNamespace(grounding_metadata=None))
    assert quellen == [] and anfragen == []


def test_chunk_ohne_uri_wird_uebersprungen():
    cand = _candidate([_chunk("Kaputt", "x.de", None), _chunk("Gut", "y.de", "https://y")])
    quellen, _ = gemini._grounding_quellen(cand)
    assert [q["domain"] for q in quellen] == ["y.de"]


# ===================== Bild-Routing kennt die neuen Aktionen =====================

def test_neue_bild_aktionen_sind_registriert():
    assert gemini._BILD_AKTIONEN["objekt_frage"][0] == "objekt_suche"
    assert gemini._BILD_AKTIONEN["objekt_kaufen"][0] == "objekt_suche"


def test_feature_ist_im_katalog():
    assert "objekt_suche" in FEATURES
    assert FEATURES["objekt_suche"].label == "Objekt erkennen"


@pytest.mark.asyncio
async def test_ohne_bild_kein_aufruf():
    assert (await gemini.objekt_erkennen(b"", "image/jpeg"))["ok"] is False
    assert (await gemini.objekt_kaufen(None, None, ""))["ok"] is False


# Registriert allein reicht nicht: der Funktionsaufruf muss auch als Aktion
# zurueckkommen. Genau daran hing es — Gemini rief objekt_frage, die
# Auswertung kannte den Namen nicht und lieferte die Rueckfrage
# "Was soll ich mit dem Bild machen?".

class _FakeModels:
    def __init__(self, name, args):
        self._name, self._args = name, args

    def generate_content(self, *, model, contents, config):
        teil = SimpleNamespace(
            function_call=SimpleNamespace(name=self._name, args=self._args))
        return SimpleNamespace(candidates=[
            SimpleNamespace(content=SimpleNamespace(parts=[teil]))])


def _fake_gemini(monkeypatch, name, args):
    monkeypatch.setattr(
        gemini, "_get_genai_client",
        lambda **kw: SimpleNamespace(models=_FakeModels(name, args)))


@pytest.mark.asyncio
async def test_router_reicht_objekt_frage_durch(monkeypatch):
    _fake_gemini(monkeypatch, "objekt_frage", {"frage": "Welcher Druck ist zulaessig?"})
    r = await gemini.route_image_intent(
        b"x" * 200, "image/jpeg", "Welcher Druck ist zulaessig?",
        features={"objekt_suche"})
    assert r == {"type": "action", "action": "objekt_frage",
                 "frage": "Welcher Druck ist zulaessig?"}


@pytest.mark.asyncio
async def test_router_reicht_objekt_kaufen_durch(monkeypatch):
    _fake_gemini(monkeypatch, "objekt_kaufen", {"beschreibung": "Dichtung fuer VC 20"})
    r = await gemini.route_image_intent(
        b"x" * 200, "image/jpeg", "Wo bekomme ich das?",
        features={"objekt_suche"})
    assert r == {"type": "action", "action": "objekt_kaufen",
                 "beschreibung": "Dichtung fuer VC 20"}


@pytest.mark.asyncio
async def test_router_ohne_frage_nimmt_den_default(monkeypatch):
    _fake_gemini(monkeypatch, "objekt_frage", {})
    r = await gemini.route_image_intent(
        b"x" * 200, "image/jpeg", "", features={"objekt_suche"})
    assert r["action"] == "objekt_frage"
    assert r["frage"] == "Was ist das genau?"


@pytest.mark.asyncio
async def test_router_ohne_feature_keine_objekt_aktion(monkeypatch):
    """Ist das Feature aus, wird die Aktion gar nicht erst angeboten."""
    _fake_gemini(monkeypatch, "objekt_frage", {"frage": "egal"})
    r = await gemini.route_image_intent(
        b"x" * 200, "image/jpeg", "Was ist das?", features={"visualisierung"})
    assert r["type"] == "message"


# ===================== Endpunkt =====================

class _Req:
    def __init__(self, body=b"", mime="image/jpeg", query=None, tenant_id=None, json_body=None):
        self._body = body
        self._json = json_body
        self.headers = {"content-type": mime}
        self.query_params = query or {}
        self.state = SimpleNamespace(
            app_tenant=SimpleNamespace(id=tenant_id or uuid.uuid4()))

    async def body(self):
        return self._body if self._json is None else b"{}"

    async def json(self):
        return self._json or {}


def _body(resp):
    return json.loads(bytes(resp.body))


def _feature(monkeypatch, an=True):
    import core.features.check as check

    async def _f(tid, key):
        return an
    monkeypatch.setattr(check, "is_feature_enabled", _f)


class _FakeSession:
    """Liefert nur die Branche, die der Endpunkt nachschlägt."""

    def __init__(self, branche):
        self._branche = branche

    async def __aenter__(self): return self
    async def __aexit__(self, *e): return False

    async def execute(self, stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: self._branche)


@pytest.mark.asyncio
async def test_endpunkt_403_ohne_feature(monkeypatch):
    _feature(monkeypatch, False)
    r = await app_screens.api_objekt_frage(request=_Req(b"x" * 200), _e=None, _c=None)
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_endpunkt_lehnt_pdf_ab(monkeypatch):
    _feature(monkeypatch)
    r = await app_screens.api_objekt_frage(
        request=_Req(b"x" * 200, mime="application/pdf"), _e=None, _c=None)
    assert r.status_code == 415


@pytest.mark.asyncio
async def test_endpunkt_lehnt_zu_grosses_bild_ab(monkeypatch):
    _feature(monkeypatch)
    r = await app_screens.api_objekt_frage(
        request=_Req(b"x" * (app_screens._OBJEKT_MAX_BYTES + 1)), _e=None, _c=None)
    assert r.status_code == 413


@pytest.mark.asyncio
async def test_endpunkt_ohne_bild(monkeypatch):
    _feature(monkeypatch)
    r = await app_screens.api_objekt_frage(request=_Req(b""), _e=None, _c=None)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_endpunkt_waehlt_kaufmodus(monkeypatch):
    _feature(monkeypatch)
    gesehen = {}

    async def _kaufen(bild, mime, beschreibung, **kw):
        gesehen["modus"] = "kaufen"
        return {"ok": True, "text": "Gibt es bei X", "quellen": [], "suchanfragen": []}

    async def _doku(bild, mime, frage, **kw):
        gesehen["modus"] = "doku"
        return {"ok": True, "text": "Das ist ein Y", "quellen": [], "suchanfragen": []}

    monkeypatch.setattr(gemini, "objekt_kaufen", _kaufen)
    monkeypatch.setattr(gemini, "objekt_erkennen", _doku)
    monkeypatch.setattr(app_screens, "get_session", lambda: _FakeSession("Schreinerei"))

    r = await app_screens.api_objekt_frage(
        request=_Req(b"x" * 200, query={"modus": "kaufen"}), _e=None, _c=None)
    assert gesehen["modus"] == "kaufen"
    assert _body(r)["modus"] == "kaufen"

    r = await app_screens.api_objekt_frage(
        request=_Req(b"x" * 200, query={"frage": "was ist das"}), _e=None, _c=None)
    assert gesehen["modus"] == "doku"
    assert _body(r)["modus"] == "doku"


@pytest.mark.asyncio
async def test_endpunkt_waehlt_anleitungsmodus(monkeypatch):
    """?modus=anleitung landet in der Anleitungs-Suche, nicht im Doku-Text.

    Das ist der Schritt, den der Handwerker nach „was ist das" geht. Fiele er
    still auf objekt_erkennen zurück, bekäme er wieder eine Erklärung statt
    des Dokuments — und der Fehler wäre von außen unsichtbar.
    """
    _feature(monkeypatch)
    gesehen = {}

    def _mach(name):
        async def _f(bild, mime, frage, **kw):
            gesehen["modus"] = name
            return {"ok": True, "text": "…", "quellen": [], "suchanfragen": []}
        return _f

    monkeypatch.setattr(gemini, "objekt_anleitung", _mach("anleitung"))
    monkeypatch.setattr(gemini, "objekt_erkennen", _mach("doku"))
    monkeypatch.setattr(gemini, "objekt_kaufen", _mach("kaufen"))
    monkeypatch.setattr(app_screens, "get_session", lambda: _FakeSession("Heizungsbau"))

    r = await app_screens.api_objekt_frage(
        request=_Req(b"x" * 200, query={"modus": "anleitung"}), _e=None, _c=None)
    assert gesehen["modus"] == "anleitung"
    assert _body(r)["modus"] == "anleitung"


@pytest.mark.asyncio
async def test_unbekannter_modus_faellt_auf_doku(monkeypatch):
    """Ein unbekannter Modus darf nicht in einer leeren Antwort enden."""
    _feature(monkeypatch)
    gesehen = {}

    async def _doku(bild, mime, frage, **kw):
        gesehen["modus"] = "doku"
        return {"ok": True, "text": "…", "quellen": [], "suchanfragen": []}

    monkeypatch.setattr(gemini, "objekt_erkennen", _doku)
    monkeypatch.setattr(app_screens, "get_session", lambda: _FakeSession(None))

    await app_screens.api_objekt_frage(
        request=_Req(b"x" * 200, query={"modus": "quatsch"}), _e=None, _c=None)
    assert gesehen["modus"] == "doku"


@pytest.mark.asyncio
async def test_anleitung_ohne_bild_und_ohne_text():
    assert (await gemini.objekt_anleitung(b"", "image/jpeg", ""))["ok"] is False


# ===================== Als Material merken =====================

class _MaterialSession:
    def __init__(self, vorhandene_slugs):
        self._slugs = vorhandene_slugs
        self.hinzugefuegt = []

    async def __aenter__(self): return self
    async def __aexit__(self, *e): return False

    async def execute(self, stmt):
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: self._slugs))

    def add(self, obj):
        self.hinzugefuegt.append(obj)

    async def commit(self): pass

    async def refresh(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()


@pytest.mark.asyncio
async def test_merken_legt_material_an(monkeypatch):
    sess = _MaterialSession([])
    monkeypatch.setattr(app_screens, "get_session", lambda: sess)
    r = await app_screens.api_objekt_merken(
        request=_Req(json_body={"name": "Vaillant Dichtung 981253",
                                "bestell_link": "https://shop.example.de/x",
                                "lieferant": "shop.example.de"}),
        emp=SimpleNamespace(id=uuid.uuid4()), _c=None)
    j = _body(r)
    assert j["ok"] is True
    assert j["slug"] == "vaillant-dichtung-981253"
    assert sess.hinzugefuegt[0].bestell_link == "https://shop.example.de/x"


@pytest.mark.asyncio
async def test_merken_macht_slug_eindeutig(monkeypatch):
    sess = _MaterialSession(["dichtung", "dichtung-2"])
    monkeypatch.setattr(app_screens, "get_session", lambda: sess)
    r = await app_screens.api_objekt_merken(
        request=_Req(json_body={"name": "Dichtung",
                                "bestell_link": "https://shop.example.de/x"}),
        emp=SimpleNamespace(id=uuid.uuid4()), _c=None)
    assert _body(r)["slug"] == "dichtung-3"


@pytest.mark.asyncio
async def test_merken_lehnt_unsinnigen_link_ab(monkeypatch):
    sess = _MaterialSession([])
    monkeypatch.setattr(app_screens, "get_session", lambda: sess)
    r = await app_screens.api_objekt_merken(
        request=_Req(json_body={"name": "Dichtung", "bestell_link": "javascript:alert(1)"}),
        emp=SimpleNamespace(id=uuid.uuid4()), _c=None)
    assert r.status_code == 400
    assert sess.hinzugefuegt == []


@pytest.mark.asyncio
async def test_merken_ohne_namen(monkeypatch):
    sess = _MaterialSession([])
    monkeypatch.setattr(app_screens, "get_session", lambda: sess)
    r = await app_screens.api_objekt_merken(
        request=_Req(json_body={"name": "", "bestell_link": "https://x.de"}),
        emp=SimpleNamespace(id=uuid.uuid4()), _c=None)
    assert r.status_code == 400
