"""Tests fuer die Verbindungen im Einstellungen-Screen der PWA
(core/api/app_screens.py: /verbindungen, /oauth/start, /lexware/verbinden,
/verbindungen/trennen).

Reine Unit-Tests mit Fakes — keine echte DB, kein Netz (Muster wie
test_app_material_bestellung.py). Die Endpoint-Coroutinen werden direkt mit
einem Fake-Request aufgerufen; die FastAPI-Depends (_e/_c) sind dabei nur
Funktionsparameter und werden mit None befuellt.

Deckt:
- Status-Mapping: Google (Kalender+Drive aus EINEM Token), Microsoft
  (available + connected), Lexware (ToolConfig)
- oauth/start: unbekannter Provider -> 400; Happy-Path liefert auth_url,
  tenant_slug/employee_slug kommen aus der Session
- lexware/verbinden: zu kurzer Key -> 400; ungueltiger Key (health_check
  wirft) -> 400; Happy-Path speichert verschluesselt + liefert Org
- verbindungen/trennen: lexware deaktiviert+Key weg; google loescht Token;
  unbekannter Provider -> 400
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from core.api import app_screens


# --------------------------------------------------------------------------
# Fakes / Helper
# --------------------------------------------------------------------------

def _req(body=None, tenant_id=None, emp_id=None, slug="pilot", emp_slug="inhaber"):
    req = SimpleNamespace()

    async def _json():
        return body or {}

    req.json = _json
    req.state = SimpleNamespace(
        app_tenant=SimpleNamespace(id=tenant_id or uuid.uuid4(), slug=slug),
        app_employee=SimpleNamespace(id=emp_id or uuid.uuid4(), slug=emp_slug),
        app_is_inhaber=True,
    )
    return req


def _body(res):
    return json.loads(res.body)


class _FakeSession:
    """Minimaler async-Context-Manager mit steuerbarem execute-Resultat."""

    def __init__(self, scalar=None):
        self._scalar = scalar
        self.added = []
        self.committed = False
        self.executed = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt):
        self.executed.append(stmt)
        return SimpleNamespace(
            scalar_one_or_none=lambda: self._scalar,
            first=lambda: self._scalar,
        )

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True


# --------------------------------------------------------------------------
# Status
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_status_google_connected_kalender_und_drive(monkeypatch):
    g = SimpleNamespace(account_email="a@gmail.com", scopes="openid,calendar,drive.file")

    async def fake_find(tid, provider, eid):
        return g if provider == "google" else None

    monkeypatch.setattr("core.security.oauth_token_lookup.find_oauth_token", fake_find)
    monkeypatch.setattr(
        "core.integrations.google_drive.is_drive_configured",
        lambda t: bool(t) and "drive" in (getattr(t, "scopes", "") or ""),
    )

    async def fake_ms():
        return False

    monkeypatch.setattr(app_screens, "_microsoft_oauth_available", fake_ms)

    lex_tc = SimpleNamespace(
        enabled=True,
        config={"encrypted_api_key": "ENC", "organization_id": "org-9"},
    )
    monkeypatch.setattr(app_screens, "get_session", lambda: _FakeSession(scalar=lex_tc))

    res = await app_screens.api_verbindungen_get(_req(), _e=None)
    b = _body(res)
    assert b["google"]["connected"] is True
    assert b["google"]["kalender"] is True
    assert b["google"]["drive"] is True
    assert b["google"]["account"] == "a@gmail.com"
    assert b["microsoft"]["connected"] is False
    assert b["microsoft"]["available"] is False
    assert b["lexware"]["connected"] is True
    assert b["lexware"]["account"] == "org-9"


@pytest.mark.asyncio
async def test_status_alles_getrennt(monkeypatch):
    async def fake_find(tid, provider, eid):
        return None

    monkeypatch.setattr("core.security.oauth_token_lookup.find_oauth_token", fake_find)
    monkeypatch.setattr(
        "core.integrations.google_drive.is_drive_configured", lambda t: False
    )

    async def fake_ms():
        return True

    monkeypatch.setattr(app_screens, "_microsoft_oauth_available", fake_ms)
    monkeypatch.setattr(app_screens, "get_session", lambda: _FakeSession(scalar=None))

    res = await app_screens.api_verbindungen_get(_req(), _e=None)
    b = _body(res)
    assert b["google"]["connected"] is False
    assert b["microsoft"]["connected"] is False
    assert b["microsoft"]["available"] is True  # einrichtbar, nur noch nicht verbunden
    assert b["lexware"]["connected"] is False


# --------------------------------------------------------------------------
# oauth/start
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_oauth_start_unbekannter_provider():
    res = await app_screens.api_oauth_start(_req({"provider": "dropbox"}), _e=None, _c=None)
    assert res.status_code == 400
    assert _body(res)["ok"] is False


@pytest.mark.asyncio
async def test_oauth_start_happy_path_session_scoped(monkeypatch):
    seen = {}

    async def fake_gen(tenant_slug, provider, employee_slug):
        seen["tenant_slug"] = tenant_slug
        seen["provider"] = provider
        seen["employee_slug"] = employee_slug
        return "https://accounts.google.com/o/oauth2/auth?state=xyz"

    monkeypatch.setattr("core.security.oauth_flow.generate_auth_url", fake_gen)

    res = await app_screens.api_oauth_start(
        _req({"provider": "google"}, slug="pilot", emp_slug="inhaber"), _e=None, _c=None
    )
    assert res.status_code == 200
    b = _body(res)
    assert b["ok"] is True
    assert b["auth_url"].startswith("https://accounts.google.com")
    # tenant_slug + employee_slug muessen aus der SESSION kommen, nicht aus dem Body
    assert seen == {"tenant_slug": "pilot", "provider": "google", "employee_slug": "inhaber"}


@pytest.mark.asyncio
async def test_oauth_start_fehler_leakt_nicht(monkeypatch):
    async def fake_gen(tenant_slug, provider, employee_slug):
        raise RuntimeError("oauth_client_secret.json fehlt")

    monkeypatch.setattr("core.security.oauth_flow.generate_auth_url", fake_gen)
    res = await app_screens.api_oauth_start(_req({"provider": "microsoft"}), _e=None, _c=None)
    assert res.status_code == 500
    assert "oauth_client_secret" not in _body(res)["error"]


# --------------------------------------------------------------------------
# lexware/verbinden
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lexware_zu_kurzer_key():
    res = await app_screens.api_lexware_verbinden(
        _req({"api_key": "kurz"}), _e=None, _c=None
    )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_lexware_ungueltiger_key_wird_nicht_gespeichert(monkeypatch):
    class _FailProvider:
        def __init__(self, api_key=None):
            pass

        async def health_check(self):
            raise RuntimeError("401 Unauthorized")

    monkeypatch.setattr("core.integrations.lexware.LexwareProvider", _FailProvider)
    sess = _FakeSession(scalar=None)
    monkeypatch.setattr(app_screens, "get_session", lambda: sess)

    res = await app_screens.api_lexware_verbinden(
        _req({"api_key": "x" * 30}), _e=None, _c=None
    )
    assert res.status_code == 400
    assert sess.committed is False  # nichts gespeichert


@pytest.mark.asyncio
async def test_lexware_happy_path_speichert_verschluesselt(monkeypatch):
    class _OkProvider:
        def __init__(self, api_key=None):
            pass

        async def health_check(self):
            return {"organizationId": "org-123"}

    monkeypatch.setattr("core.integrations.lexware.LexwareProvider", _OkProvider)
    monkeypatch.setattr("core.security.encryption.encrypt", lambda s: "ENC:" + s)
    sess = _FakeSession(scalar=None)  # noch keine ToolConfig -> neu anlegen
    monkeypatch.setattr(app_screens, "get_session", lambda: sess)

    key = "k" * 30
    res = await app_screens.api_lexware_verbinden(
        _req({"api_key": key}), _e=None, _c=None
    )
    assert res.status_code == 200
    assert _body(res)["account"] == "org-123"
    assert sess.committed is True
    assert len(sess.added) == 1
    tc = sess.added[0]
    assert tc.config["encrypted_api_key"] == "ENC:" + key
    assert tc.config["organization_id"] == "org-123"
    assert tc.enabled is True


# --------------------------------------------------------------------------
# verbindungen/trennen
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_trennen_unbekannter_provider():
    res = await app_screens.api_verbindungen_trennen(
        _req({"provider": "dropbox"}), _e=None, _c=None
    )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_trennen_lexware_deaktiviert_und_entfernt_key(monkeypatch):
    tc = SimpleNamespace(enabled=True, config={"encrypted_api_key": "ENC", "organization_id": "o"})
    sess = _FakeSession(scalar=tc)
    monkeypatch.setattr(app_screens, "get_session", lambda: sess)

    res = await app_screens.api_verbindungen_trennen(
        _req({"provider": "lexware"}), _e=None, _c=None
    )
    assert res.status_code == 200
    assert tc.enabled is False
    assert "encrypted_api_key" not in tc.config
    assert sess.committed is True


@pytest.mark.asyncio
async def test_trennen_google_loescht_token(monkeypatch):
    tok = SimpleNamespace(id=uuid.uuid4())

    async def fake_find(tid, provider, eid):
        return tok

    monkeypatch.setattr("core.security.oauth_token_lookup.find_oauth_token", fake_find)
    sess = _FakeSession(scalar=None)
    monkeypatch.setattr(app_screens, "get_session", lambda: sess)

    res = await app_screens.api_verbindungen_trennen(
        _req({"provider": "google"}), _e=None, _c=None
    )
    assert res.status_code == 200
    assert sess.committed is True
    assert len(sess.executed) == 1  # ein DELETE abgesetzt


@pytest.mark.asyncio
async def test_trennen_google_ohne_token_ist_idempotent(monkeypatch):
    async def fake_find(tid, provider, eid):
        return None

    monkeypatch.setattr("core.security.oauth_token_lookup.find_oauth_token", fake_find)
    sess = _FakeSession(scalar=None)
    monkeypatch.setattr(app_screens, "get_session", lambda: sess)

    res = await app_screens.api_verbindungen_trennen(
        _req({"provider": "microsoft"}), _e=None, _c=None
    )
    assert res.status_code == 200
    assert sess.committed is False  # nichts zu loeschen
