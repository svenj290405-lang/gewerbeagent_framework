"""Tests fuer den OAuth-Token-Invalid-Alarm.

Verifiziert:
- notify_oauth_token_invalid baut den richtigen Re-Auth-Link
- Throttling: 2. Aufruf innerhalb 6h wird unterdrueckt
- Push-Text enthaelt die wichtigen Elemente (Link, Anleitung, Sven-Hinweis)
- Google bekommt die "unverified app"-Anleitung, Microsoft nicht
- is_oauth_invalid_error erkennt google + microsoft Patterns
- tenant_alert.notify_oauth_revoked delegiert auf den neuen Helper
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.security import oauth_alert


@pytest.fixture(autouse=True)
def _reset_throttle():
    """Jeder Test startet mit leerem Throttle-Cache."""
    oauth_alert._reset_throttle_for_tests()
    yield
    oauth_alert._reset_throttle_for_tests()


@pytest.fixture
def push_capture(monkeypatch):
    """Faengt TelegramNotifier.send_for_tenant ab."""
    calls: list[dict] = []

    async def fake_send(tenant_id, text, *, employee_id=None):
        calls.append({
            "tenant_id": tenant_id, "text": text,
            "employee_id": employee_id,
        })
        return True

    import plugins.telegram_notify.handler as tnh
    monkeypatch.setattr(tnh.TelegramNotifier, "send_for_tenant", fake_send)
    return calls


@pytest.fixture
def tenant_in_db(monkeypatch):
    """Mockt AsyncSessionLocal so dass Tenant-Lookup einen Demo-Tenant
    liefert (slug=demo, company_name="Schreinerei Test")."""
    tenant = SimpleNamespace(
        id=uuid.uuid4(),
        slug="demo",
        company_name="Schreinerei Test GbR",
    )

    class _FakeResult:
        def scalar_one_or_none(self):
            return tenant

    class _FakeSession:
        async def execute(self, stmt):
            return _FakeResult()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    def fake_sessionlocal():
        return _FakeSession()

    from core import database as db_mod
    monkeypatch.setattr(db_mod, "AsyncSessionLocal", fake_sessionlocal)
    return tenant


# =====================================================================
# notify_oauth_token_invalid — Inhalt + Throttling
# =====================================================================

@pytest.mark.asyncio
async def test_google_push_contains_reauth_link_and_warn_advice(
    push_capture, tenant_in_db,
):
    """Google-Push: Re-Auth-URL, Schritte fuer Verifizierungs-Warnung,
    Sven-Hinweis."""
    sent = await oauth_alert.notify_oauth_token_invalid(
        tenant_in_db.id, "google", reason="invalid_grant",
    )
    assert sent is True
    assert len(push_capture) == 1
    text = push_capture[0]["text"]
    # Re-Auth-URL mit Tenant-Slug + Provider
    # URL ist HTML-escaped (`&` -> `&amp;`)
    assert "tenant=demo" in text and "provider=google" in text
    # Google-Schritte erwaehnt
    assert "Erweitert" in text
    assert "nicht verifiziert" in text
    # Sven-Kontakt-Hinweis
    assert "Sven" in text
    # Drive + Kalender Label
    assert "Drive" in text and "Kalender" in text


@pytest.mark.asyncio
async def test_microsoft_push_has_no_unverified_warning(
    push_capture, tenant_in_db,
):
    """Microsoft hat kein 'unverified app'-Theater — Push soll auch
    keinen entsprechenden Hinweis enthalten."""
    await oauth_alert.notify_oauth_token_invalid(
        tenant_in_db.id, "microsoft",
    )
    text = push_capture[0]["text"]
    assert "Outlook" in text or "Microsoft" in text
    assert "tenant=demo" in text and "provider=microsoft" in text
    # KEIN Verifizierungs-Theater fuer Microsoft
    assert "nicht verifiziert" not in text
    assert "Erweitert" not in text


@pytest.mark.asyncio
async def test_throttle_suppresses_second_push_within_window(
    push_capture, tenant_in_db,
):
    """Zweiter Aufruf innerhalb 6h wird unterdrueckt (False return)."""
    first = await oauth_alert.notify_oauth_token_invalid(
        tenant_in_db.id, "google",
    )
    second = await oauth_alert.notify_oauth_token_invalid(
        tenant_in_db.id, "google",
    )
    assert first is True
    assert second is False
    assert len(push_capture) == 1


@pytest.mark.asyncio
async def test_throttle_keys_per_provider(push_capture, tenant_in_db):
    """Throttling ist pro (tenant, provider) — google + microsoft sind
    unabhaengig."""
    await oauth_alert.notify_oauth_token_invalid(tenant_in_db.id, "google")
    await oauth_alert.notify_oauth_token_invalid(tenant_in_db.id, "microsoft")
    assert len(push_capture) == 2


@pytest.mark.asyncio
async def test_unknown_provider_skipped(push_capture, tenant_in_db):
    ok = await oauth_alert.notify_oauth_token_invalid(
        tenant_in_db.id, "lexware",
    )
    assert ok is False
    assert len(push_capture) == 0


# =====================================================================
# is_oauth_invalid_error
# =====================================================================

def test_is_oauth_invalid_detects_google_invalid_grant():
    exc = Exception(
        "('invalid_grant: Token has been expired or revoked.', {...})"
    )
    assert oauth_alert.is_oauth_invalid_error(exc) is True


def test_is_oauth_invalid_detects_microsoft_aadsts70043():
    exc = Exception("AADSTS70043: The refresh token has expired ...")
    assert oauth_alert.is_oauth_invalid_error(exc) is True


def test_is_oauth_invalid_ignores_random_error():
    exc = Exception("Connection refused: db not reachable")
    assert oauth_alert.is_oauth_invalid_error(exc) is False


# =====================================================================
# tenant_alert.notify_oauth_revoked delegiert auf oauth_alert
# =====================================================================

@pytest.mark.asyncio
async def test_tenant_alert_wrapper_delegates(monkeypatch, tenant_in_db):
    """notify_oauth_revoked (alter API-Pfad fuer Microsoft-Refresh)
    soll auf oauth_alert.notify_oauth_token_invalid weiterleiten."""
    spy = AsyncMock(return_value=True)
    monkeypatch.setattr(
        oauth_alert, "notify_oauth_token_invalid", spy,
    )
    from core.integrations import tenant_alert
    await tenant_alert.notify_oauth_revoked(
        tenant_id=tenant_in_db.id, provider="microsoft",
    )
    spy.assert_awaited_once()
    args, kwargs = spy.call_args
    assert args[0] == tenant_in_db.id
    assert args[1] == "microsoft"
