"""Tests fuer die PWA-Auth-/Sicherheits-Schicht (core/security/app_auth.py,
core/api/app_routes.py, core/integrations/push_notifier.py).

Reine Unit-Tests mit Fakes — keine DB noetig (gleiches Muster wie
test_employee_activation.py).

Deckt die sicherheitskritischen Punkte:
- CSRF-Validierung (Header + Form, constant-time, Ablehnung)
- Session-Cookie-Flags (HttpOnly, Secure, SameSite, Path=/app)
- find_employee_by_email: ungueltige Adressen werden vor jeder DB-Query
  abgewiesen
- Inhaber-Gate (require_app_inhaber)
- Push: deaktiviert ohne VAPID-Keys; Payload bleibt inhaltslos/minimal
"""
from __future__ import annotations

import secrets
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.security import app_auth


# =====================================================================
# Fakes
# =====================================================================

class _FakeHeaders(dict):
    def get(self, k, default=None):
        return super().get(k.lower(), default)


def _make_request(*, csrf_token="tok", header=None, form=None, cookies=None):
    headers = _FakeHeaders()
    if header is not None:
        headers[app_auth.CSRF_HEADER_NAME.lower()] = header
    req = SimpleNamespace()
    req.state = SimpleNamespace(app_session=SimpleNamespace(csrf_token=csrf_token))
    req.headers = headers
    req.cookies = cookies or {}
    req.client = SimpleNamespace(host="127.0.0.1")

    async def _form():
        return form or {}
    req.form = _form
    return req


# =====================================================================
# CSRF
# =====================================================================

@pytest.mark.asyncio
async def test_csrf_accepts_matching_header():
    req = _make_request(csrf_token="abc", header="abc")
    # darf nicht werfen
    await app_auth.require_app_csrf(req)


@pytest.mark.asyncio
async def test_csrf_accepts_matching_form():
    req = _make_request(csrf_token="abc", header=None, form={app_auth.CSRF_FIELD_NAME: "abc"})
    await app_auth.require_app_csrf(req)


@pytest.mark.asyncio
async def test_csrf_rejects_mismatch():
    from fastapi import HTTPException
    req = _make_request(csrf_token="abc", header="WRONG")
    with pytest.raises(HTTPException) as ei:
        await app_auth.require_app_csrf(req)
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_csrf_rejects_missing():
    from fastapi import HTTPException
    req = _make_request(csrf_token="abc", header=None, form={})
    with pytest.raises(HTTPException):
        await app_auth.require_app_csrf(req)


@pytest.mark.asyncio
async def test_csrf_requires_session():
    from fastapi import HTTPException
    req = SimpleNamespace(state=SimpleNamespace(), headers=_FakeHeaders())
    with pytest.raises(HTTPException):
        await app_auth.require_app_csrf(req)


# =====================================================================
# Session-Cookie-Flags
# =====================================================================

def test_session_cookie_is_hardened():
    captured = {}

    class _Resp:
        def set_cookie(self, **kw):
            captured.update(kw)

    app_auth.set_app_session_cookie(_Resp(), "tokenvalue")
    assert captured["key"] == app_auth.APP_SESSION_COOKIE_NAME
    assert captured["httponly"] is True
    assert captured["samesite"] == "strict"
    assert captured["path"] == "/app"
    # value darf nicht leer sein
    assert captured["value"] == "tokenvalue"


# =====================================================================
# E-Mail-Normalisierung / Schutz vor DB-Query bei Muell-Input
# =====================================================================

@pytest.mark.asyncio
async def test_find_employee_rejects_invalid_email_without_query():
    session = AsyncMock()
    # ungueltig (kein @) -> None, OHNE execute aufzurufen
    res = await app_auth.find_employee_by_email("keinatzeichen", session=session)
    assert res is None
    session.execute.assert_not_called()


@pytest.mark.asyncio
async def test_find_employee_rejects_overlong_email():
    session = AsyncMock()
    res = await app_auth.find_employee_by_email("a@" + "x" * 300, session=session)
    assert res is None
    session.execute.assert_not_called()


# =====================================================================
# Inhaber-Gate
# =====================================================================

@pytest.mark.asyncio
async def test_require_inhaber_blocks_non_default(monkeypatch):
    from fastapi import HTTPException
    emp = SimpleNamespace(is_default=False)

    async def _fake_user(_request):
        return emp
    monkeypatch.setattr(app_auth, "require_app_user", _fake_user)
    with pytest.raises(HTTPException) as ei:
        await app_auth.require_app_inhaber(SimpleNamespace())
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_require_inhaber_allows_default(monkeypatch):
    emp = SimpleNamespace(is_default=True)

    async def _fake_user(_request):
        return emp
    monkeypatch.setattr(app_auth, "require_app_user", _fake_user)
    out = await app_auth.require_app_inhaber(SimpleNamespace())
    assert out is emp


# =====================================================================
# Push: deaktiviert ohne VAPID-Keys
# =====================================================================

@pytest.mark.asyncio
async def test_push_disabled_without_keys(monkeypatch):
    from core.integrations import push_notifier
    monkeypatch.setattr(push_notifier.settings, "vapid_public_key", "", raising=False)
    monkeypatch.setattr(push_notifier.settings, "vapid_private_key", "", raising=False)
    assert push_notifier.push_enabled() is False
    sent = await push_notifier.send_push_to_employee(
        secrets.token_hex(8), title="x", body="y",
    )
    assert sent == 0


# =====================================================================
# Push-Subscribe: Re-Bind bleibt im eigenen Betrieb
# =====================================================================

class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    """Minimal-Session: liefert der Reihe nach vorgegebene Ergebnisse."""

    def __init__(self, ergebnisse):
        self._ergebnisse = list(ergebnisse)
        self.geloescht = []
        self.angelegt = []
        self.abfragen = []

    async def execute(self, stmt):
        self.abfragen.append(stmt)
        return _FakeResult(
            self._ergebnisse.pop(0) if self._ergebnisse else None
        )

    async def delete(self, obj):
        self.geloescht.append(obj)

    async def flush(self):
        pass

    def add(self, obj):
        self.angelegt.append(obj)


def _push_request(tenant_id, emp_id):
    req = SimpleNamespace()
    req.state = SimpleNamespace(app_employee=SimpleNamespace(id=emp_id))
    req.headers = _FakeHeaders()

    async def _json():
        return {"subscription": {
            "endpoint": "https://push.example/abc",
            "keys": {"p256dh": "p" * 20, "auth": "a" * 10},
        }}
    req.json = _json
    return req


def _patch_push_route(monkeypatch, session, tenant_id):
    from core.api import app_routes
    import contextlib

    @contextlib.asynccontextmanager
    async def _fake_session():
        yield session
    monkeypatch.setattr(app_routes, "get_session", _fake_session)
    monkeypatch.setattr(app_routes, "current_tenant_id", lambda _r: tenant_id)
    return app_routes


@pytest.mark.asyncio
async def test_push_subscribe_uebernimmt_fremde_zeile_nicht(monkeypatch):
    """Endpoint gehoert einem ANDEREN Tenant: alte Zeile muss weg, neue her.

    Vorher wurde die fremde Zeile einfach auf den eigenen Tenant
    umgeschrieben — das Geraet waere still in den fremden Betrieb
    gewandert und haette dessen Pushes bekommen.
    """
    eigener = uuid.uuid4()
    fremder = uuid.uuid4()
    fremde_zeile = SimpleNamespace(tenant_id=fremder, employee_id=uuid.uuid4())
    # 1. Abfrage (eigener Tenant) -> nichts, 2. Abfrage (global) -> fremde Zeile
    session = _FakeSession([None, fremde_zeile])
    app_routes = _patch_push_route(monkeypatch, session, eigener)

    emp_id = uuid.uuid4()
    resp = await app_routes.app_push_subscribe(_push_request(eigener, emp_id))

    assert resp.status_code == 200
    assert session.geloescht == [fremde_zeile], "fremde Zeile muss geloescht werden"
    assert len(session.angelegt) == 1
    assert session.angelegt[0].tenant_id == eigener
    assert session.angelegt[0].employee_id == emp_id
    assert fremde_zeile.tenant_id == fremder, "fremde Zeile darf nicht umgebogen werden"


@pytest.mark.asyncio
async def test_push_subscribe_rebind_im_eigenen_betrieb(monkeypatch):
    """Gleiches Geraet, anderer Kollege im selben Betrieb: Zeile wird uebernommen."""
    tenant = uuid.uuid4()
    alt_emp = uuid.uuid4()
    eigene_zeile = SimpleNamespace(
        tenant_id=tenant, employee_id=alt_emp,
        p256dh="", auth="", user_agent=None,
    )
    session = _FakeSession([eigene_zeile])
    app_routes = _patch_push_route(monkeypatch, session, tenant)

    neu_emp = uuid.uuid4()
    resp = await app_routes.app_push_subscribe(_push_request(tenant, neu_emp))

    assert resp.status_code == 200
    assert session.geloescht == []
    assert session.angelegt == []
    assert eigene_zeile.employee_id == neu_emp
    assert eigene_zeile.tenant_id == tenant
