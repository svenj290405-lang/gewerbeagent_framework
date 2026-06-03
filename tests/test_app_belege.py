"""Tests fuer den Beleg-Upload der PWA
(core/api/app_screens.py: /belege/upload + Helfer).

Reine Unit-Tests mit Fakes — keine echte DB, kein Lexware-Call (gleiches
Muster wie test_app_diktat.py). Der Lexware-Provider wird gemockt,
get_session durch eine Fake-Session ersetzt.

Deckt:
- MIME-Whitelist (jpeg/png/pdf ok, sonst None)
- Endpunkt: leerer Body -> 400, zu gross -> 413, falscher MIME -> 415,
  Lexware nicht verbunden -> 409
- Happy-Path: Provider.upload_voucher_file wird aufgerufen, ok + Deeplink
- Idempotenz: schon hochgeladener Hash -> duplikat, ohne erneuten Upload
- Lexware-Fehler -> 502
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from core.api import app_screens
from core.integrations.accounting_base import AccountingError
from core.models.beleg import BELEG_STATUS_UPLOADED


# =====================================================================
# Reiner Helfer
# =====================================================================

@pytest.mark.parametrize("raw,expected", [
    ("image/jpeg", "image/jpeg"),
    ("image/png", "image/png"),
    ("application/pdf", "application/pdf"),
    ("image/jpeg; charset=binary", "image/jpeg"),
    ("IMAGE/PNG", "image/png"),
    ("image/heic", None),
    ("image/webp", None),
    ("", None),
    (None, None),
])
def test_normalize_beleg_mime(raw, expected):
    assert app_screens._normalize_beleg_mime(raw) == expected


# =====================================================================
# Fakes
# =====================================================================

class _FakeHeaders(dict):
    def get(self, k, default=None):
        return super().get(k.lower(), default)


class _FakeBelegSession:
    """get_session()-Ersatz. Ein store-weiter Zaehler unterscheidet den
    ersten execute() (Idempotenz-Lookup -> store['existing']) von spaeteren
    (Refetch by id -> das zuvor hinzugefuegte Objekt)."""
    def __init__(self, store):
        self.store = store

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt):
        self.store["calls"] = self.store.get("calls", 0) + 1
        val = self.store.get("existing") if self.store["calls"] == 1 else self.store.get("added")
        return SimpleNamespace(scalar_one_or_none=lambda v=val: v)

    def add(self, obj):
        self.store["added"] = obj

    async def commit(self):
        obj = self.store.get("added")
        if obj is not None and getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()

    async def refresh(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()


class _FakeProvider:
    def __init__(self, result=None, exc=None):
        self.result = result
        self.exc = exc
        self.called = False

    async def upload_voucher_file(self, file_bytes, mime_type, filename=None):
        self.called = True
        if self.exc is not None:
            raise self.exc
        return self.result


def _make_request(body, *, content_type="image/jpeg", caption="", filename="q.jpg", tenant_id=None):
    req = SimpleNamespace()

    async def _body():
        return body
    req.body = _body
    headers = _FakeHeaders()
    if content_type is not None:
        headers["content-type"] = content_type
    req.headers = headers
    req.query_params = {"caption": caption, "filename": filename}
    req.state = SimpleNamespace(app_tenant=SimpleNamespace(id=tenant_id or uuid.uuid4()))
    return req


def _patch(monkeypatch, *, provider="__default__", existing=None):
    store = {}
    if existing is not None:
        store["existing"] = existing
    monkeypatch.setattr(app_screens, "get_session", lambda: _FakeBelegSession(store))
    if provider == "__default__":
        provider = _FakeProvider(
            result=SimpleNamespace(file_id=uuid.uuid4(), voucher_id=uuid.uuid4())
        )

    async def _build(_tid):
        return provider
    monkeypatch.setattr(app_screens, "_build_lexware_provider", _build)
    return store, provider


def _emp():
    return SimpleNamespace(id=uuid.uuid4())


def _json(resp):
    return json.loads(bytes(resp.body))


# =====================================================================
# Endpunkt
# =====================================================================

@pytest.mark.asyncio
async def test_beleg_empty_body_returns_400(monkeypatch):
    _patch(monkeypatch)
    resp = await app_screens.api_beleg_upload(request=_make_request(b""), emp=_emp(), _c=None)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_beleg_oversized_returns_413(monkeypatch):
    _patch(monkeypatch)
    big = b"x" * (app_screens._BELEG_MAX_SIZE_BYTES + 1)
    resp = await app_screens.api_beleg_upload(request=_make_request(big), emp=_emp(), _c=None)
    assert resp.status_code == 413


@pytest.mark.asyncio
async def test_beleg_bad_mime_returns_415(monkeypatch):
    _patch(monkeypatch)
    req = _make_request(b"x" * 1000, content_type="image/heic")
    resp = await app_screens.api_beleg_upload(request=req, emp=_emp(), _c=None)
    assert resp.status_code == 415


@pytest.mark.asyncio
async def test_beleg_no_lexware_returns_409(monkeypatch):
    _patch(monkeypatch, provider=None)
    resp = await app_screens.api_beleg_upload(
        request=_make_request(b"x" * 1000), emp=_emp(), _c=None,
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_beleg_happy_path_uploads_and_returns_link(monkeypatch):
    store, provider = _patch(monkeypatch)
    req = _make_request(b"x" * 2000, caption="Bauhaus Schrauben", filename="quittung.jpg")
    resp = await app_screens.api_beleg_upload(request=req, emp=_emp(), _c=None)
    assert resp.status_code == 200
    j = _json(resp)
    assert j["ok"] is True
    assert j.get("lexware_link")
    assert provider.called is True
    saved = store["added"]
    assert saved.source == "api"
    assert saved.caption == "Bauhaus Schrauben"
    assert saved.file_mime == "image/jpeg"
    assert saved.status == BELEG_STATUS_UPLOADED   # finales Update


@pytest.mark.asyncio
async def test_beleg_duplicate_short_circuits(monkeypatch):
    existing = SimpleNamespace(
        id=uuid.uuid4(), status=BELEG_STATUS_UPLOADED,
        lexware_voucher_id=uuid.uuid4(),
    )
    store, provider = _patch(monkeypatch, existing=existing)
    resp = await app_screens.api_beleg_upload(
        request=_make_request(b"x" * 2000), emp=_emp(), _c=None,
    )
    assert resp.status_code == 200
    j = _json(resp)
    assert j["ok"] is True and j["duplikat"] is True
    assert provider.called is False          # KEIN erneuter Upload
    assert "added" not in store               # nichts neu angelegt


@pytest.mark.asyncio
async def test_beleg_lexware_error_returns_502(monkeypatch):
    provider = _FakeProvider(exc=AccountingError("boom", status_code=503))
    _patch(monkeypatch, provider=provider)
    resp = await app_screens.api_beleg_upload(
        request=_make_request(b"x" * 2000), emp=_emp(), _c=None,
    )
    assert resp.status_code == 502
    assert provider.called is True
