"""Tests fuer das Browser-Sprach-Diktat der PWA
(core/api/app_screens.py: /aufnahmen/diktat + Helfer).

Reine Unit-Tests mit Fakes — keine echte DB, kein Gemini-Call (gleiches
Muster wie test_app_auth.py). Gemini wird gemockt, get_session durch eine
Fake-Session ersetzt, die das gespeicherte Objekt einfaengt.

Deckt:
- Validierung der Roh-Audiodaten (leer / zu gross)
- MIME-Whitelist (wav ok, webm/opus abgelehnt)
- Dauer-Parsing inkl. Plausibilitaets-Cap
- Termin-Parsing aus der Gemini-Extraktion
- Endpunkt: Happy-Path speichert ein Kundengespraech mit korrektem
  Feld-Mapping + Mitarbeiter-Zuordnung
- Endpunkt: kein Kundenname -> 422, nicht unterstuetztes Format -> 415,
  leerer Body -> 400, Gemini-Fehler -> 502
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from core.api import app_screens


# =====================================================================
# Reine Helfer
# =====================================================================

def test_validate_audio_rejects_empty():
    err = app_screens._validate_diktat_audio(b"")
    assert err is not None and err[1] == 400


def test_validate_audio_rejects_oversized():
    big = b"x" * (app_screens._DIKTAT_MAX_AUDIO_BYTES + 1)
    err = app_screens._validate_diktat_audio(big)
    assert err is not None and err[1] == 413


def test_validate_audio_accepts_normal():
    assert app_screens._validate_diktat_audio(b"x" * 5000) is None


@pytest.mark.parametrize("raw,expected", [
    ("audio/wav", "audio/wav"),
    ("audio/wav; codecs=1", "audio/wav"),
    ("AUDIO/WAV", "audio/wav"),
    ("audio/ogg", "audio/ogg"),
    ("audio/webm;codecs=opus", None),   # Roh-MediaRecorder-Default: abgelehnt
    ("audio/mp4", None),
    ("", None),
    (None, None),
])
def test_normalize_mime(raw, expected):
    assert app_screens._normalize_diktat_mime(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("12", 12),
    ("12.9", 12),
    (None, None),
    ("", None),
    ("abc", None),
    ("-5", None),
    (str(24 * 3600 + 1), None),   # ueber Cap
    (str(24 * 3600), 24 * 3600),
])
def test_parse_duration(raw, expected):
    assert app_screens._parse_diktat_duration(raw) == expected


def test_parse_termin_iso():
    d = app_screens._parse_diktat_termin("2026-06-05T09:00:00")
    assert d is not None and d.year == 2026 and d.month == 6 and d.day == 5
    assert d.tzinfo is not None


def test_parse_termin_space_format():
    d = app_screens._parse_diktat_termin("2026-06-05 09:00")
    assert d is not None and d.hour == 9


def test_parse_termin_date_only():
    d = app_screens._parse_diktat_termin("2026-06-05")
    assert d is not None and d.day == 5


def test_parse_termin_unparseable():
    assert app_screens._parse_diktat_termin("naechste Woche") is None
    assert app_screens._parse_diktat_termin(None) is None


# =====================================================================
# Fakes fuer den Endpunkt
# =====================================================================

class _FakeHeaders(dict):
    def get(self, k, default=None):
        return super().get(k.lower(), default)


class _FakeSession:
    """Async-Context-Manager-Ersatz fuer get_session(). Faengt das
    hinzugefuegte Objekt ein und simuliert das DB-seitige id-Default."""
    def __init__(self, store):
        self.store = store

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def add(self, obj):
        self.store["added"] = obj

    async def commit(self):
        obj = self.store.get("added")
        if obj is not None and getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()


def _make_request(body, *, content_type="audio/wav", duration="12", tenant_id=None):
    req = SimpleNamespace()

    async def _body():
        return body
    req.body = _body
    headers = _FakeHeaders()
    if content_type is not None:
        headers["content-type"] = content_type
    if duration is not None:
        headers["x-audio-duration"] = duration
    req.headers = headers
    req.state = SimpleNamespace(
        app_tenant=SimpleNamespace(id=tenant_id or uuid.uuid4())
    )
    return req


def _patch_save(monkeypatch):
    store = {}
    monkeypatch.setattr(app_screens, "get_session", lambda: _FakeSession(store))
    return store


def _patch_gemini(monkeypatch, result=None, exc=None):
    import core.ai

    async def _fake(audio_bytes, mime_type="audio/ogg", *, tenant_id=None):
        if exc is not None:
            raise exc
        return result or {}
    monkeypatch.setattr(core.ai, "analyse_kundengespraech_from_audio", _fake)


def _body_json(resp):
    return json.loads(bytes(resp.body))


# =====================================================================
# Endpunkt
# =====================================================================

@pytest.mark.asyncio
async def test_diktat_happy_path_saves_gespraech(monkeypatch):
    store = _patch_save(monkeypatch)
    _patch_gemini(monkeypatch, result={
        "kunde_name": "Frau Mueller",
        "briefing_kurz": "Treppenlift im 2. Stock.",
        "notizen_lang": "Lange Notizen ...",
        "todos": ["Wasserwaage mitbringen"],
        "termin_datum": "2026-06-05T09:00:00",
        "termin_ort": "Hauptstr. 42",
        "transcript": "wortwoertlich ...",
        "extraction_confidence": "high",
    })
    tid = uuid.uuid4()
    emp = SimpleNamespace(id=uuid.uuid4())
    req = _make_request(b"x" * 5000, tenant_id=tid, duration="42")

    resp = await app_screens.api_aufnahme_diktat(request=req, emp=emp, _c=None)
    assert resp.status_code == 200
    j = _body_json(resp)
    assert j["ok"] is True
    assert j["kunde"] == "Frau Mueller"
    assert j["todos"] == ["Wasserwaage mitbringen"]

    saved = store["added"]
    assert saved.tenant_id == tid
    assert saved.kunde_name == "Frau Mueller"
    assert saved.status == "erfasst"
    assert saved.audio_dauer_sekunden == 42
    assert saved.created_by_employee_id == emp.id
    assert saved.assigned_employee_id == emp.id
    assert saved.termin_datum is not None and saved.termin_datum.day == 5
    assert saved.confidence == "high"


@pytest.mark.asyncio
async def test_diktat_missing_kunde_name_returns_422(monkeypatch):
    _patch_save(monkeypatch)
    _patch_gemini(monkeypatch, result={"kunde_name": "", "briefing_kurz": "x"})
    req = _make_request(b"x" * 5000)
    resp = await app_screens.api_aufnahme_diktat(
        request=req, emp=SimpleNamespace(id=uuid.uuid4()), _c=None,
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_diktat_unsupported_mime_returns_415(monkeypatch):
    _patch_save(monkeypatch)
    _patch_gemini(monkeypatch, result={"kunde_name": "X"})
    req = _make_request(b"x" * 5000, content_type="audio/webm;codecs=opus")
    resp = await app_screens.api_aufnahme_diktat(
        request=req, emp=SimpleNamespace(id=uuid.uuid4()), _c=None,
    )
    assert resp.status_code == 415


@pytest.mark.asyncio
async def test_diktat_empty_body_returns_400(monkeypatch):
    _patch_save(monkeypatch)
    _patch_gemini(monkeypatch, result={"kunde_name": "X"})
    req = _make_request(b"")
    resp = await app_screens.api_aufnahme_diktat(
        request=req, emp=SimpleNamespace(id=uuid.uuid4()), _c=None,
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_diktat_gemini_failure_returns_502(monkeypatch):
    _patch_save(monkeypatch)
    _patch_gemini(monkeypatch, exc=RuntimeError("vertex down"))
    req = _make_request(b"x" * 5000)
    resp = await app_screens.api_aufnahme_diktat(
        request=req, emp=SimpleNamespace(id=uuid.uuid4()), _c=None,
    )
    assert resp.status_code == 502
