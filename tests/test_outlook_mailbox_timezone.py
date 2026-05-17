"""Tests fuer die Outlook-Mailbox-Timezone-Warnung (Onboarding Option 2).

Deckt:
- is_berlin_compatible_timezone: Akzeptanz-Set + None-Fallback
- get_mailbox_timezone: Success (Plain-String + Wrapped-JSON), 403, Crash
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import httpx
import pytest

from core.integrations.microsoft_calendar import (
    BERLIN_COMPATIBLE_MAILBOX_TIMEZONES,
    get_mailbox_timezone,
    is_berlin_compatible_timezone,
)


# =====================================================================
# is_berlin_compatible_timezone
# =====================================================================

@pytest.mark.parametrize("tz", [
    "W. Europe Standard Time",
    "Central European Standard Time",
    "Europe/Berlin",
    "Europe/Vienna",
    "Europe/Zurich",
    "Europe/Amsterdam",
    "Europe/Paris",
    "Romance Standard Time",
])
def test_compatible_timezones_return_true(tz):
    assert is_berlin_compatible_timezone(tz) is True


@pytest.mark.parametrize("tz", [
    "UTC",
    "Pacific Standard Time",
    "Eastern Standard Time",
    "America/New_York",
    "Asia/Tokyo",
    "GMT Standard Time",  # London — selbe Offset im Winter, Sommer aber -1h
])
def test_incompatible_timezones_return_false(tz):
    assert is_berlin_compatible_timezone(tz) is False


def test_none_timezone_treated_as_compatible():
    """None bedeutet 'wissen wir nicht' — nicht falsch warnen."""
    assert is_berlin_compatible_timezone(None) is True


def test_compatible_set_is_immutable():
    """frozenset damit niemand zur Laufzeit Eintraege addiert."""
    assert isinstance(BERLIN_COMPATIBLE_MAILBOX_TIMEZONES, frozenset)


# =====================================================================
# get_mailbox_timezone
# =====================================================================

class _FakeResp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no payload")
        return self._payload


class _FakeClient:
    """Async-Context-Manager-Stub fuer httpx.AsyncClient."""

    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def get(self, url, headers=None):
        return self._resp


def _patch_token(monkeypatch, token="ya29.fake"):
    from core.integrations import microsoft_calendar as mc
    monkeypatch.setattr(mc, "get_microsoft_token", AsyncMock(return_value=token))


@pytest.mark.asyncio
async def test_get_mailbox_timezone_returns_string_value(monkeypatch):
    """Graph liefert den Wert als nackten JSON-String wie 'W. Europe Standard Time'."""
    from core.integrations import microsoft_calendar as mc
    _patch_token(monkeypatch)
    monkeypatch.setattr(
        mc.httpx, "AsyncClient",
        lambda **kw: _FakeClient(_FakeResp(200, payload="W. Europe Standard Time")),
    )
    tz = await get_mailbox_timezone(uuid.uuid4())
    assert tz == "W. Europe Standard Time"


@pytest.mark.asyncio
async def test_get_mailbox_timezone_returns_none_on_403(monkeypatch):
    """Scope nicht granted -> 403 -> None (Caller darf nicht warnen)."""
    from core.integrations import microsoft_calendar as mc
    _patch_token(monkeypatch)
    monkeypatch.setattr(
        mc.httpx, "AsyncClient",
        lambda **kw: _FakeClient(_FakeResp(403, text='{"error": "denied"}')),
    )
    tz = await get_mailbox_timezone(uuid.uuid4())
    assert tz is None


@pytest.mark.asyncio
async def test_get_mailbox_timezone_returns_none_on_token_error(monkeypatch):
    """OAuth-Token-Lookup failt -> None, kein Crash."""
    from core.integrations import microsoft_calendar as mc
    monkeypatch.setattr(
        mc, "get_microsoft_token",
        AsyncMock(side_effect=RuntimeError("kein Token")),
    )
    tz = await get_mailbox_timezone(uuid.uuid4())
    assert tz is None


@pytest.mark.asyncio
async def test_get_mailbox_timezone_returns_none_on_http_crash(monkeypatch):
    """Netzwerkfehler waehrend Graph-Call -> None, kein Crash."""
    from core.integrations import microsoft_calendar as mc
    _patch_token(monkeypatch)

    class _BoomClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def get(self, *a, **kw):
            raise httpx.ConnectError("boom")

    monkeypatch.setattr(
        mc.httpx, "AsyncClient", lambda **kw: _BoomClient(),
    )
    tz = await get_mailbox_timezone(uuid.uuid4())
    assert tz is None


# =====================================================================
# Smoke: MICROSOFT_SCOPES enthaelt MailboxSettings.Read
# =====================================================================

def test_microsoft_scopes_include_mailboxsettings_read():
    from core.security.oauth_flow import MICROSOFT_SCOPES
    assert "MailboxSettings.Read" in MICROSOFT_SCOPES
