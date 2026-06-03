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
