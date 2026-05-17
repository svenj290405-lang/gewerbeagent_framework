"""Tests fuer /microsoft_status Anzeige (Refactor 2026-05-17).

Hintergrund: vorher zeigte der Status "Token gueltig bis 17:32" — das
ist der ~1h kurzlebige Access-Token-Verfall und alarmiert User unnoetig
("oh, mein Token laeuft gleich ab!"), obwohl Auto-Refresh transparent
laeuft.

Neue Anzeige:
- "Verbunden seit DD.MM.YYYY"   (created_at)
- "Letzter Auto-Refresh: vor X Min"   (updated_at)
- "🔄 Auto-Refresh aktiv ..."   (explanatory hint)
- KEINE access_token-Verfallszeit mehr

get_microsoft_status liefert connected_since + last_refresh zusaetzlich
zu den bisherigen Feldern (Backward-Compat: existierende Caller die
nur connected/account_email lesen brechen nicht).
"""
from __future__ import annotations

import datetime as dt
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.telegram_notify import handler as tn


# =====================================================================
# Doubles
# =====================================================================

def _make_tenant():
    return SimpleNamespace(id=uuid.uuid4(), slug="demo")


def _patch_tenant(monkeypatch, tenant):
    monkeypatch.setattr(tn, "_get_tenant_by_chat",
                        AsyncMock(return_value=tenant))


def _patch_status(monkeypatch, status_dict):
    """Patch get_microsoft_status mit fixem Dict."""
    import core.integrations.microsoft as ms
    monkeypatch.setattr(
        ms, "get_microsoft_status",
        AsyncMock(return_value=status_dict),
    )


# =====================================================================
# get_microsoft_status: neue Felder
# =====================================================================

@pytest.mark.asyncio
async def test_status_dict_includes_connected_since_and_last_refresh(monkeypatch):
    """Result-Dict muss connected_since + last_refresh enthalten."""
    import core.integrations.microsoft as ms

    created = dt.datetime(2026, 5, 1, 10, 0, tzinfo=dt.timezone.utc)
    updated = dt.datetime(2026, 5, 17, 14, 0, tzinfo=dt.timezone.utc)
    fake_token = SimpleNamespace(
        account_email="user@example.com",
        access_token_expires_at=dt.datetime(2026, 5, 17, 15, 0, tzinfo=dt.timezone.utc),
        scopes="Mail.Send Calendars.ReadWrite",
        created_at=created,
        updated_at=updated,
    )
    monkeypatch.setattr(
        "core.security.oauth_token_lookup.find_oauth_token",
        AsyncMock(return_value=fake_token),
    )
    result = await ms.get_microsoft_status(uuid.uuid4())
    assert result["connected"] is True
    assert result["connected_since"] == created
    assert result["last_refresh"] == updated
    assert result["account_email"] == "user@example.com"


@pytest.mark.asyncio
async def test_status_dict_when_not_connected_has_all_none(monkeypatch):
    """Wenn kein Token: alle neuen Felder explizit None (kein KeyError)."""
    import core.integrations.microsoft as ms
    monkeypatch.setattr(
        "core.security.oauth_token_lookup.find_oauth_token",
        AsyncMock(return_value=None),
    )
    result = await ms.get_microsoft_status(uuid.uuid4())
    assert result["connected"] is False
    assert result["connected_since"] is None
    assert result["last_refresh"] is None


# =====================================================================
# /microsoft_status Telegram-Anzeige
# =====================================================================

@pytest.mark.asyncio
async def test_status_message_omits_access_token_expiry(monkeypatch):
    """Die kryptische access_token-Verfallszeit darf NICHT mehr erscheinen."""
    _patch_tenant(monkeypatch, _make_tenant())
    _patch_status(monkeypatch, {
        "connected": True,
        "account_email": "user@example.com",
        "expires_at": dt.datetime(2026, 5, 17, 15, 32, tzinfo=dt.timezone.utc),
        "scopes": "Mail.Send",
        "connected_since": dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc),
        "last_refresh": dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10),
    })
    reply = await tn._handle_microsoft_status_command(chat_id=1)
    assert "Token gueltig bis" not in reply
    assert "15:32" not in reply


@pytest.mark.asyncio
async def test_status_message_shows_connected_since(monkeypatch):
    _patch_tenant(monkeypatch, _make_tenant())
    _patch_status(monkeypatch, {
        "connected": True,
        "account_email": "user@example.com",
        "expires_at": None,
        "scopes": "Mail.Send",
        "connected_since": dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc),
        "last_refresh": dt.datetime.now(dt.timezone.utc),
    })
    reply = await tn._handle_microsoft_status_command(chat_id=1)
    assert "Verbunden seit" in reply
    assert "01.05.2026" in reply


@pytest.mark.asyncio
async def test_status_message_shows_last_refresh_minutes_ago(monkeypatch):
    _patch_tenant(monkeypatch, _make_tenant())
    _patch_status(monkeypatch, {
        "connected": True,
        "account_email": "user@example.com",
        "expires_at": None,
        "scopes": "Mail.Send",
        "connected_since": dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc),
        "last_refresh": dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=42),
    })
    reply = await tn._handle_microsoft_status_command(chat_id=1)
    assert "Letzter Auto-Refresh" in reply
    assert "vor 42 Min" in reply


@pytest.mark.asyncio
async def test_status_message_shows_last_refresh_hours_ago(monkeypatch):
    _patch_tenant(monkeypatch, _make_tenant())
    _patch_status(monkeypatch, {
        "connected": True,
        "account_email": "user@example.com",
        "expires_at": None,
        "scopes": "Mail.Send",
        "connected_since": dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc),
        "last_refresh": dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=3),
    })
    reply = await tn._handle_microsoft_status_command(chat_id=1)
    assert "vor 3 Std" in reply


@pytest.mark.asyncio
async def test_status_message_explains_auto_refresh(monkeypatch):
    """Klare Aussage dass die Verbindung sich selbst erneuert — sonst
    versteht der Handwerker nicht warum 'Letzter Refresh vor 8 Std' OK ist."""
    _patch_tenant(monkeypatch, _make_tenant())
    _patch_status(monkeypatch, {
        "connected": True,
        "account_email": "user@example.com",
        "expires_at": None,
        "scopes": "Mail.Send",
        "connected_since": dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc),
        "last_refresh": dt.datetime.now(dt.timezone.utc),
    })
    reply = await tn._handle_microsoft_status_command(chat_id=1)
    assert "Auto-Refresh aktiv" in reply


@pytest.mark.asyncio
async def test_status_message_not_connected(monkeypatch):
    _patch_tenant(monkeypatch, _make_tenant())
    _patch_status(monkeypatch, {
        "connected": False,
        "account_email": None,
        "expires_at": None,
        "scopes": None,
        "connected_since": None,
        "last_refresh": None,
    })
    reply = await tn._handle_microsoft_status_command(chat_id=1)
    assert "nicht verbunden" in reply.lower()
    assert "/microsoft_setup" in reply


@pytest.mark.asyncio
async def test_status_message_handles_missing_timestamps_gracefully(monkeypatch):
    """Wenn die DB beide Timestamps NULL hat (theoretisch unmoeglich, aber
    Defensiv): kein Crash, Anzeige bleibt verstaendlich."""
    _patch_tenant(monkeypatch, _make_tenant())
    _patch_status(monkeypatch, {
        "connected": True,
        "account_email": "user@example.com",
        "expires_at": None,
        "scopes": "Mail.Send",
        "connected_since": None,
        "last_refresh": None,
    })
    reply = await tn._handle_microsoft_status_command(chat_id=1)
    assert "verbunden" in reply.lower()
    assert "user@example.com" in reply
    assert "Verbunden seit" not in reply
    assert "Letzter Auto-Refresh" not in reply
