"""Tests fuer den /neue_termine Telegram-Befehl.

/neue_termine zeigt nur Kalender-Events deren event_id beim letzten
Aufruf nicht in der Baseline-Liste war. Erster Aufruf zeigt alle
und legt die Baseline an.
"""
from __future__ import annotations

import datetime as dt
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.telegram_notify import handler as tn


# =====================================================================
# Test-Doubles
# =====================================================================

def _make_employee(*, provider="microsoft"):
    return SimpleNamespace(
        id=uuid.uuid4(), slug="emp",
        is_default=True, is_active=True,
        calendar_provider=provider,
    )


def _make_tenant():
    return SimpleNamespace(id=uuid.uuid4(), slug="demo")


def _make_event(*, event_id, start, subject, location="", web_link=""):
    return {
        "start_dt": start,
        "end_dt": start + dt.timedelta(hours=1),
        "subject": subject,
        "location": location,
        "event_id": event_id,
        "body_preview": "",
        "web_link": web_link,
    }


def _patch_baseline(monkeypatch, *, initial_seen=None):
    """Mockt get/set fuer die Seen-Baseline mit einem in-memory store."""
    store = {"ids": list(initial_seen or [])}

    async def fake_get(chat_id):
        return set(store["ids"])

    async def fake_set(chat_id, ids):
        store["ids"] = list(ids)

    monkeypatch.setattr(tn, "_get_termine_seen_event_ids", fake_get)
    monkeypatch.setattr(tn, "_set_termine_seen_event_ids", fake_set)
    return store


# =====================================================================
# Tests
# =====================================================================

@pytest.mark.asyncio
async def test_neue_termine_no_chat_assignment(monkeypatch):
    monkeypatch.setattr(tn, "_get_current_employee", AsyncMock(return_value=None))
    reply = await tn._handle_neue_termine_command(chat_id=12345)
    assert "noch keinem betrieb" in reply.lower()


@pytest.mark.asyncio
async def test_neue_termine_no_calendar(monkeypatch):
    tenant = _make_tenant()
    emp = _make_employee(provider=None)
    monkeypatch.setattr(tn, "_get_current_employee",
                        AsyncMock(return_value=(tenant, emp)))
    reply = await tn._handle_neue_termine_command(chat_id=12345)
    assert "/kalender_verbinden" in reply


@pytest.mark.asyncio
async def test_neue_termine_first_call_shows_all_and_marks_baseline(monkeypatch):
    """Erster Aufruf: leere Baseline -> alle Events zeigen +
    Baseline auf aktuelle Liste setzen."""
    tenant = _make_tenant()
    emp = _make_employee()
    monkeypatch.setattr(tn, "_get_current_employee",
                        AsyncMock(return_value=(tenant, emp)))

    ev1 = _make_event(event_id="evt-1",
                      start=dt.datetime(2026, 5, 18, 9, 0),
                      subject="Erstgespraech")
    ev2 = _make_event(event_id="evt-2",
                      start=dt.datetime(2026, 5, 19, 14, 0),
                      subject="Montage")
    fetch_calls = {"n": 0}

    async def fake_fetch(t, e, d):
        fetch_calls["n"] += 1
        return ([ev1, ev2] if fetch_calls["n"] == 1 else []), "Outlook"

    monkeypatch.setattr(tn, "_fetch_calendar_events_for_day", fake_fetch)
    store = _patch_baseline(monkeypatch, initial_seen=[])

    reply = await tn._handle_neue_termine_command(chat_id=42)
    assert "Erstgespraech" in reply
    assert "Montage" in reply
    assert "Erster Aufruf" in reply
    assert set(store["ids"]) == {"evt-1", "evt-2"}


@pytest.mark.asyncio
async def test_neue_termine_second_call_with_same_list_shows_nothing(monkeypatch):
    tenant = _make_tenant()
    emp = _make_employee()
    monkeypatch.setattr(tn, "_get_current_employee",
                        AsyncMock(return_value=(tenant, emp)))

    ev1 = _make_event(event_id="evt-1",
                      start=dt.datetime(2026, 5, 18, 9, 0),
                      subject="Bekannt-1")
    ev2 = _make_event(event_id="evt-2",
                      start=dt.datetime(2026, 5, 19, 14, 0),
                      subject="Bekannt-2")
    fetch_calls = {"n": 0}

    async def fake_fetch(t, e, d):
        fetch_calls["n"] += 1
        return ([ev1, ev2] if fetch_calls["n"] == 1 else []), "Outlook"

    monkeypatch.setattr(tn, "_fetch_calendar_events_for_day", fake_fetch)
    _patch_baseline(monkeypatch, initial_seen=["evt-1", "evt-2"])

    reply = await tn._handle_neue_termine_command(chat_id=42)
    assert "Keine neuen Termine" in reply
    assert "Bekannt-1" not in reply
    assert "Bekannt-2" not in reply


@pytest.mark.asyncio
async def test_neue_termine_filters_to_new_only(monkeypatch):
    """Mix aus bekannt + neu -> nur das Neue zeigen."""
    tenant = _make_tenant()
    emp = _make_employee()
    monkeypatch.setattr(tn, "_get_current_employee",
                        AsyncMock(return_value=(tenant, emp)))

    old = _make_event(event_id="old-1",
                      start=dt.datetime(2026, 5, 18, 9, 0),
                      subject="Bekannt")
    new = _make_event(event_id="new-1",
                      start=dt.datetime(2026, 5, 18, 14, 0),
                      subject="Neu Gebuchter Termin",
                      web_link="https://outlook.example/abc")
    fetch_calls = {"n": 0}

    async def fake_fetch(t, e, d):
        fetch_calls["n"] += 1
        return ([old, new] if fetch_calls["n"] == 1 else []), "Outlook"

    monkeypatch.setattr(tn, "_fetch_calendar_events_for_day", fake_fetch)
    store = _patch_baseline(monkeypatch, initial_seen=["old-1"])

    reply = await tn._handle_neue_termine_command(chat_id=42)
    assert "Neu Gebuchter Termin" in reply
    assert "Bekannt" not in reply
    assert "Neue Termine seit dem letzten Aufruf" in reply
    assert '<a href="https://outlook.example/abc">' in reply
    assert set(store["ids"]) == {"old-1", "new-1"}


@pytest.mark.asyncio
async def test_neue_termine_removed_event_shrinks_baseline(monkeypatch):
    """Termin geloescht -> Baseline schrumpft, wird nicht als 'neu' angezeigt."""
    tenant = _make_tenant()
    emp = _make_employee()
    monkeypatch.setattr(tn, "_get_current_employee",
                        AsyncMock(return_value=(tenant, emp)))

    survivor = _make_event(event_id="survivor",
                           start=dt.datetime(2026, 5, 18, 9, 0),
                           subject="Bleibt")
    fetch_calls = {"n": 0}

    async def fake_fetch(t, e, d):
        fetch_calls["n"] += 1
        return ([survivor] if fetch_calls["n"] == 1 else []), "Outlook"

    monkeypatch.setattr(tn, "_fetch_calendar_events_for_day", fake_fetch)
    store = _patch_baseline(monkeypatch, initial_seen=["gone-1", "gone-2", "survivor"])

    reply = await tn._handle_neue_termine_command(chat_id=42)
    assert "Keine neuen Termine" in reply
    assert set(store["ids"]) == {"survivor"}


@pytest.mark.asyncio
async def test_neue_termine_truncates_to_max(monkeypatch):
    """Mehr als MAX neue Events -> Truncate-Hinweis."""
    tenant = _make_tenant()
    emp = _make_employee()
    monkeypatch.setattr(tn, "_get_current_employee",
                        AsyncMock(return_value=(tenant, emp)))

    many = [
        _make_event(
            event_id=f"e-{i:02d}",
            start=dt.datetime(2026, 5, 18, 8 + i // 6, (i * 10) % 60),
            subject=f"Termin {i:02d}",
        )
        for i in range(30)
    ]
    fetch_calls = {"n": 0}

    async def fake_fetch(t, e, d):
        fetch_calls["n"] += 1
        return (many if fetch_calls["n"] == 1 else []), "Outlook"

    monkeypatch.setattr(tn, "_fetch_calendar_events_for_day", fake_fetch)
    _patch_baseline(monkeypatch, initial_seen=[])

    reply = await tn._handle_neue_termine_command(chat_id=42)
    assert "ausgeblendet" in reply.lower()
    appearances = sum(1 for i in range(30) if f"Termin {i:02d}" in reply)
    assert appearances == tn.TERMINE_MAX_ENTRIES
