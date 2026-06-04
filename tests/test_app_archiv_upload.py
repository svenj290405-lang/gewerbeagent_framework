"""Tests fuer den Kunden-Archiv-Upload der PWA
(core/api/app_screens.py: POST /archiv/upload + /archiv/notiz).

Reine Unit-Tests mit Fakes — keine echte DB, kein Drive, kein Netz (Muster
wie test_app_verbindungen.py). Die wiederverwendete Drive-Funktion
upload_file_to_kunde_folder wird gepatcht; is_feature_enabled ebenfalls.

Deckt:
- upload: Feature aus -> 403, kein Body -> 400, zu gross -> 413, falscher
  MIME -> 415, fehlender Kunde -> 400, Happy-Path ruft Drive-Upload + liefert
  folder_url/upload_count, ?caption= legt zusaetzlich Notiz an, ValueError
  (keine Drive-Verbindung) -> 409
- notiz: Feature aus -> 403, leerer Text -> 400, Happy-Path legt .txt an
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from core.api import app_screens


def _req(*, body=b"", content_type="application/octet-stream", query=None,
         json_body=None, tid=None):
    req = SimpleNamespace()

    async def _b():
        return body

    async def _j():
        return json_body or {}

    req.body = _b
    req.json = _j
    req.headers = {"content-type": content_type}
    req.query_params = query or {}
    req.state = SimpleNamespace(
        app_tenant=SimpleNamespace(id=tid or uuid.uuid4()),
        app_employee=SimpleNamespace(id=uuid.uuid4()),
    )
    return req


def _emp():
    return SimpleNamespace(id=uuid.uuid4())


def _body(res):
    return json.loads(res.body)


def _feat(monkeypatch, on: bool):
    async def fake(tid, key):
        return on
    monkeypatch.setattr("core.features.check.is_feature_enabled", fake)


def _patch_upload(monkeypatch, calls, result=None, raises=None):
    async def fake(**kw):
        calls.append(kw)
        if raises is not None:
            raise raises
        return result or {
            "file_id": "f1", "web_link": "https://drive/file",
            "kunde_folder_id": "fo1", "kunde_folder_url": "https://drive/folder",
            "upload_count": 3,
        }
    monkeypatch.setattr("core.integrations.google_drive.upload_file_to_kunde_folder", fake)


# --------------------------------------------------------------------------
# /archiv/upload
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upload_feature_aus_403(monkeypatch):
    _feat(monkeypatch, False)
    res = await app_screens.api_archiv_upload(
        _req(body=b"x", content_type="image/jpeg", query={"kunde_name": "Mueller"}),
        emp=_emp(), _c=None)
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_upload_kein_body_400(monkeypatch):
    _feat(monkeypatch, True)
    res = await app_screens.api_archiv_upload(
        _req(body=b"", content_type="image/jpeg", query={"kunde_name": "Mueller"}),
        emp=_emp(), _c=None)
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_upload_zu_gross_413(monkeypatch):
    _feat(monkeypatch, True)
    monkeypatch.setattr(app_screens, "_ARCHIV_MAX_SIZE_BYTES", 10)
    res = await app_screens.api_archiv_upload(
        _req(body=b"x" * 11, content_type="image/jpeg", query={"kunde_name": "Mueller"}),
        emp=_emp(), _c=None)
    assert res.status_code == 413


@pytest.mark.asyncio
async def test_upload_falscher_mime_415(monkeypatch):
    _feat(monkeypatch, True)
    res = await app_screens.api_archiv_upload(
        _req(body=b"x", content_type="application/zip", query={"kunde_name": "Mueller"}),
        emp=_emp(), _c=None)
    assert res.status_code == 415


@pytest.mark.asyncio
async def test_upload_fehlender_kunde_400(monkeypatch):
    _feat(monkeypatch, True)
    res = await app_screens.api_archiv_upload(
        _req(body=b"x", content_type="image/jpeg", query={}),
        emp=_emp(), _c=None)
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_upload_happy_path(monkeypatch):
    _feat(monkeypatch, True)
    calls = []
    _patch_upload(monkeypatch, calls)
    res = await app_screens.api_archiv_upload(
        _req(body=b"JPEGDATA", content_type="image/jpeg",
             query={"kunde_name": "Mueller", "kunde_email": "m@x.de", "filename": "dach.jpg"}),
        emp=_emp(), _c=None)
    assert res.status_code == 200
    b = _body(res)
    assert b["ok"] is True
    assert b["folder_url"] == "https://drive/folder"
    assert b["upload_count"] == 3
    assert len(calls) == 1
    assert calls[0]["kunde_name"] == "Mueller"
    assert calls[0]["mime_type"] == "image/jpeg"
    assert calls[0]["kunde_email"] == "m@x.de"
    assert calls[0]["filename"] == "dach.jpg"
    assert calls[0]["file_bytes"] == b"JPEGDATA"


@pytest.mark.asyncio
async def test_upload_mit_caption_legt_zusaetzlich_notiz_an(monkeypatch):
    _feat(monkeypatch, True)
    calls = []
    _patch_upload(monkeypatch, calls)
    res = await app_screens.api_archiv_upload(
        _req(body=b"PNGDATA", content_type="image/png",
             query={"kunde_name": "Mueller", "caption": "Riss in der Wand"}),
        emp=_emp(), _c=None)
    assert res.status_code == 200
    # 1x Datei + 1x Notiz (text/plain)
    assert len(calls) == 2
    mimes = {c["mime_type"] for c in calls}
    assert "image/png" in mimes and "text/plain" in mimes
    note_call = next(c for c in calls if c["mime_type"] == "text/plain")
    assert b"Riss in der Wand" in note_call["file_bytes"]


@pytest.mark.asyncio
async def test_upload_notiz_fehler_kippt_datei_nicht(monkeypatch):
    _feat(monkeypatch, True)
    calls = []
    # erster Call ok (Datei), zweiter (Notiz) wirft -> Antwort bleibt 200
    async def fake(**kw):
        calls.append(kw)
        if kw["mime_type"] == "text/plain":
            raise RuntimeError("notiz kaputt")
        return {"kunde_folder_url": "https://drive/folder", "upload_count": 1}
    monkeypatch.setattr("core.integrations.google_drive.upload_file_to_kunde_folder", fake)
    res = await app_screens.api_archiv_upload(
        _req(body=b"PNGDATA", content_type="image/png",
             query={"kunde_name": "Mueller", "caption": "x"}),
        emp=_emp(), _c=None)
    assert res.status_code == 200
    assert _body(res)["ok"] is True


@pytest.mark.asyncio
async def test_upload_keine_drive_verbindung_409(monkeypatch):
    _feat(monkeypatch, True)
    calls = []
    _patch_upload(monkeypatch, calls, raises=ValueError("kein drive scope"))
    res = await app_screens.api_archiv_upload(
        _req(body=b"x", content_type="image/jpeg", query={"kunde_name": "Mueller"}),
        emp=_emp(), _c=None)
    assert res.status_code == 409
    assert "Drive" in _body(res)["error"]


# --------------------------------------------------------------------------
# /archiv/notiz
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_notiz_feature_aus_403(monkeypatch):
    _feat(monkeypatch, False)
    res = await app_screens.api_archiv_notiz(
        _req(json_body={"kunde_name": "Mueller", "text": "hallo"}), emp=_emp(), _c=None)
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_notiz_leerer_text_400(monkeypatch):
    _feat(monkeypatch, True)
    res = await app_screens.api_archiv_notiz(
        _req(json_body={"kunde_name": "Mueller", "text": ""}), emp=_emp(), _c=None)
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_notiz_happy_path(monkeypatch):
    _feat(monkeypatch, True)
    calls = []
    _patch_upload(monkeypatch, calls, result={"kunde_folder_url": "https://drive/folder", "upload_count": 5})
    res = await app_screens.api_archiv_notiz(
        _req(json_body={"kunde_name": "Mueller", "text": "Termin verschoben", "kunde_email": "m@x.de"}),
        emp=_emp(), _c=None)
    assert res.status_code == 200
    b = _body(res)
    assert b["ok"] is True and b["upload_count"] == 5
    assert len(calls) == 1
    assert calls[0]["mime_type"] == "text/plain"
    assert b"Termin verschoben" in calls[0]["file_bytes"]
    assert calls[0]["filename"].startswith("notiz_") and calls[0]["filename"].endswith(".txt")
