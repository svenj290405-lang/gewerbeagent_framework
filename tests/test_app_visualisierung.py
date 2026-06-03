"""Tests fuer die Visualisierung der PWA
(core/api/app_screens.py: /visualisierungen + /{id}/bild + Helfer).

Reine Unit-Tests mit Fakes — keine echte DB, kein Gemini-Bild-Call
(Muster wie test_app_diktat.py). generate_image_from_image + Feature-Gate
+ get_session werden gemockt.

Deckt:
- MIME-Whitelist (jpeg/png ok, sonst None)
- POST: Feature aus -> 403, leerer Body -> 400, zu gross -> 413, falscher
  MIME -> 415, zu kurzer Prompt -> 400
- POST Happy-Path: Bild generiert, status=done, bild_url zurueck
- POST Fehlschlag (kein Bild) -> 502, status=failed
- GET bild: ungueltige id -> 400, nicht gefunden -> 404
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from core.api import app_screens
from core.models.visualisierung import VIZ_STATUS_DONE, VIZ_STATUS_FAILED


@pytest.mark.parametrize("raw,expected", [
    ("image/jpeg", "image/jpeg"),
    ("image/png", "image/png"),
    ("image/jpeg; x=1", "image/jpeg"),
    ("IMAGE/PNG", "image/png"),
    ("image/webp", None),
    ("application/pdf", None),
    (None, None),
])
def test_normalize_viz_mime(raw, expected):
    assert app_screens._normalize_viz_mime(raw) == expected


# =====================================================================
# Fakes
# =====================================================================

class _FakeHeaders(dict):
    def get(self, k, default=None):
        return super().get(k.lower(), default)


class _FakeVizSession:
    def __init__(self, store):
        self.store = store

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def add(self, obj):
        self.store["added"] = obj

    async def commit(self):
        pass

    async def refresh(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()

    async def execute(self, stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: self.store.get("added"))


def _req(body=b"", *, content_type="image/jpeg", prompt="Wände grau streichen", tenant_id=None):
    req = SimpleNamespace()

    async def _body():
        return body
    req.body = _body
    headers = _FakeHeaders()
    if content_type is not None:
        headers["content-type"] = content_type
    req.headers = headers
    req.query_params = {"prompt": prompt}
    req.state = SimpleNamespace(app_tenant=SimpleNamespace(id=tenant_id or uuid.uuid4()))
    return req


def _emp():
    return SimpleNamespace(id=uuid.uuid4())


def _json(resp):
    return json.loads(bytes(resp.body))


def _patch(monkeypatch, *, feature=True, gen_result=b"", gen_exc=None):
    store = {}
    monkeypatch.setattr(app_screens, "get_session", lambda: _FakeVizSession(store))

    async def _feat(_tid, _key):
        return feature
    monkeypatch.setattr(app_screens, "_feature_enabled", _feat)

    import core.ai

    async def _gen(image_bytes, prompt, mime_type="image/jpeg", model="gemini-2.5-flash-image"):
        if gen_exc is not None:
            raise gen_exc
        return gen_result
    monkeypatch.setattr(core.ai, "generate_image_from_image", _gen)
    return store


# =====================================================================
# POST /visualisierungen
# =====================================================================

@pytest.mark.asyncio
async def test_viz_feature_disabled_returns_403(monkeypatch):
    _patch(monkeypatch, feature=False)
    resp = await app_screens.api_visualisierung_erstellen(
        request=_req(b"x" * 100), emp=_emp(), _c=None,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_viz_empty_body_returns_400(monkeypatch):
    _patch(monkeypatch)
    resp = await app_screens.api_visualisierung_erstellen(
        request=_req(b""), emp=_emp(), _c=None,
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_viz_oversized_returns_413(monkeypatch):
    _patch(monkeypatch)
    big = b"x" * (app_screens._VIZ_MAX_BYTES + 1)
    resp = await app_screens.api_visualisierung_erstellen(
        request=_req(big), emp=_emp(), _c=None,
    )
    assert resp.status_code == 413


@pytest.mark.asyncio
async def test_viz_bad_mime_returns_415(monkeypatch):
    _patch(monkeypatch)
    resp = await app_screens.api_visualisierung_erstellen(
        request=_req(b"x" * 100, content_type="image/webp"), emp=_emp(), _c=None,
    )
    assert resp.status_code == 415


@pytest.mark.asyncio
async def test_viz_short_prompt_returns_400(monkeypatch):
    _patch(monkeypatch)
    resp = await app_screens.api_visualisierung_erstellen(
        request=_req(b"x" * 100, prompt="hi"), emp=_emp(), _c=None,
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_viz_happy_path(monkeypatch):
    store = _patch(monkeypatch, gen_result=b"\x89PNG-fake-bytes")
    resp = await app_screens.api_visualisierung_erstellen(
        request=_req(b"x" * 200), emp=_emp(), _c=None,
    )
    assert resp.status_code == 200
    j = _json(resp)
    assert j["ok"] is True
    assert j["bild_url"].endswith("/bild")
    viz = store["added"]
    assert viz.status == VIZ_STATUS_DONE
    assert viz.result_image_data == b"\x89PNG-fake-bytes"


@pytest.mark.asyncio
async def test_viz_no_image_returns_502_and_marks_failed(monkeypatch):
    store = _patch(monkeypatch, gen_result=None)
    resp = await app_screens.api_visualisierung_erstellen(
        request=_req(b"x" * 200), emp=_emp(), _c=None,
    )
    assert resp.status_code == 502
    assert store["added"].status == VIZ_STATUS_FAILED


@pytest.mark.asyncio
async def test_viz_generation_exception_returns_502(monkeypatch):
    store = _patch(monkeypatch, gen_exc=RuntimeError("vertex down"))
    resp = await app_screens.api_visualisierung_erstellen(
        request=_req(b"x" * 200), emp=_emp(), _c=None,
    )
    assert resp.status_code == 502
    assert store["added"].status == VIZ_STATUS_FAILED


# =====================================================================
# GET /visualisierungen/{id}/bild
# =====================================================================

@pytest.mark.asyncio
async def test_bild_invalid_id_returns_400(monkeypatch):
    monkeypatch.setattr(app_screens, "get_session", lambda: _FakeVizSession({}))
    resp = await app_screens.api_visualisierung_bild(
        vid="kein-uuid", request=_req(), _e=None,
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_bild_not_found_returns_404(monkeypatch):
    # leerer Store -> scalar_one_or_none() -> None
    monkeypatch.setattr(app_screens, "get_session", lambda: _FakeVizSession({}))
    resp = await app_screens.api_visualisierung_bild(
        vid=str(uuid.uuid4()), request=_req(), _e=None,
    )
    assert resp.status_code == 404
