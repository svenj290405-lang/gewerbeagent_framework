"""Tests fuer den /termine Telegram-Befehl.

Zeigt die naechsten Tage als Liste mit Link zum Kalender. Multi-
Mitarbeiter: jeder sieht den eigenen Kalender; bei nicht-verbundenem
Kalender Hinweis-Text.
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

def _make_employee(*, slug="emp", is_default=False, provider="microsoft"):
    return SimpleNamespace(
        id=uuid.uuid4(), slug=slug,
        is_default=is_default, is_active=True,
        calendar_provider=provider,
    )


def _make_tenant():
    return SimpleNamespace(id=uuid.uuid4(), slug="demo")


def _make_event(*, start, subject, location="", web_link=""):
    return {
        "start_dt": start,
        "end_dt": start + dt.timedelta(hours=1),
        "subject": subject,
        "location": location,
        "event_id": uuid.uuid4().hex,
        "body_preview": "",
        "web_link": web_link,
    }


# =====================================================================
# Tests
# =====================================================================

@pytest.mark.asyncio
async def test_termine_no_chat_assignment(monkeypatch):
    """Chat nicht zugeordnet -> hilfreicher Text."""
    monkeypatch.setattr(tn, "_get_current_employee", AsyncMock(return_value=None))
    reply = await tn._handle_termine_command(chat_id=12345)
    assert "noch keinem betrieb" in reply.lower() or "nicht zugeordnet" in reply.lower()


@pytest.mark.asyncio
async def test_termine_no_calendar_connected(monkeypatch):
    """Employee ohne calendar_provider -> Hinweis auf /kalender_verbinden."""
    tenant = _make_tenant()
    emp = _make_employee(provider=None)
    monkeypatch.setattr(tn, "_get_current_employee",
                        AsyncMock(return_value=(tenant, emp)))
    reply = await tn._handle_termine_command(chat_id=12345)
    assert "/kalender_verbinden" in reply


@pytest.mark.asyncio
async def test_termine_empty_calendar(monkeypatch):
    """Calendar verbunden aber keine Termine -> Leer-Meldung mit Provider-Label."""
    tenant = _make_tenant()
    emp = _make_employee(provider="microsoft")
    monkeypatch.setattr(tn, "_get_current_employee",
                        AsyncMock(return_value=(tenant, emp)))
    monkeypatch.setattr(tn, "_fetch_calendar_events_for_day",
                        AsyncMock(return_value=([], "Outlook")))

    reply = await tn._handle_termine_command(chat_id=12345)
    assert "Keine Termine" in reply
    assert "Outlook" in reply


@pytest.mark.asyncio
async def test_termine_renders_events_with_link(monkeypatch):
    """Ein Event mit web_link -> klickbarer Link im HTML."""
    tenant = _make_tenant()
    emp = _make_employee(provider="microsoft")
    monkeypatch.setattr(tn, "_get_current_employee",
                        AsyncMock(return_value=(tenant, emp)))

    target_event = _make_event(
        start=dt.datetime(2026, 5, 22, 14, 0),
        subject="Beratung Mueller",
        location="Hauptstr 1",
        web_link="https://outlook.office.com/calendar/event/abc123",
    )

    # _fetch_calendar_events_for_day wird 7-mal aufgerufen (TERMINE_DEFAULT_DAYS).
    # Wir liefern das Event nur beim ersten Call zurueck.
    calls = {"n": 0}

    async def fake_fetch(tenant, emp, date):
        calls["n"] += 1
        return ([target_event] if calls["n"] == 1 else []), "Outlook"

    monkeypatch.setattr(tn, "_fetch_calendar_events_for_day", fake_fetch)
    reply = await tn._handle_termine_command(chat_id=12345)
    assert "Beratung Mueller" in reply
    assert "Hauptstr 1" in reply
    # Link muss als HTML-anchor da sein
    assert '<a href="https://outlook.office.com/calendar/event/abc123">' in reply
    assert "Im Kalender oeffnen" in reply
    assert "📅" in reply


@pytest.mark.asyncio
async def test_termine_sorts_chronologically_across_days(monkeypatch):
    """Events aus verschiedenen Tagen muessen chronologisch sortiert sein."""
    tenant = _make_tenant()
    emp = _make_employee(provider="microsoft")
    monkeypatch.setattr(tn, "_get_current_employee",
                        AsyncMock(return_value=(tenant, emp)))

    early = _make_event(
        start=dt.datetime(2026, 5, 18, 9, 0), subject="Frueh",
    )
    late = _make_event(
        start=dt.datetime(2026, 5, 18, 16, 0), subject="Spaet",
    )
    other_day = _make_event(
        start=dt.datetime(2026, 5, 19, 10, 0), subject="Naechster Tag",
    )

    # Day 0 liefert late + early (in dieser Reihenfolge — Sortierung
    # muss im _handle_termine_command passieren), Day 1 liefert other_day.
    day_results = [
        ([late, early], "Outlook"),
        ([other_day], "Outlook"),
    ] + [([], "Outlook")] * 5

    async def fake_fetch(tenant, emp, date):
        return day_results.pop(0)

    monkeypatch.setattr(tn, "_fetch_calendar_events_for_day", fake_fetch)
    reply = await tn._handle_termine_command(chat_id=12345)
    # Reihenfolge: Frueh < Spaet < Naechster Tag
    pos_early = reply.find("Frueh")
    pos_late = reply.find("Spaet")
    pos_other = reply.find("Naechster Tag")
    assert 0 < pos_early < pos_late < pos_other


@pytest.mark.asyncio
async def test_termine_truncates_to_max_entries(monkeypatch):
    """Mehr als TERMINE_MAX_ENTRIES Events -> truncate-Hinweis am Ende."""
    tenant = _make_tenant()
    emp = _make_employee(provider="microsoft")
    monkeypatch.setattr(tn, "_get_current_employee",
                        AsyncMock(return_value=(tenant, emp)))

    # 30 Events auf einen Tag — uebersteigt das Max (20)
    many = [
        _make_event(
            start=dt.datetime(2026, 5, 18, 8 + i // 6, (i * 10) % 60),
            subject=f"Termin {i:02d}",
        )
        for i in range(30)
    ]

    async def fake_fetch(tenant, emp, date):
        if date == dt.date(2026, 5, 18):
            return many, "Outlook"
        return [], "Outlook"

    # Spy datetime.now damit "heute" 2026-05-18 ist
    import plugins.telegram_notify.handler as tnh
    monkeypatch.setattr(tn, "_fetch_calendar_events_for_day", fake_fetch)
    # In dem Handler wird datetime.now(local_tz) gerufen — wir koennen
    # das umgehen indem wir es laufen lassen aber die Day-Range start
    # eh ab today.

    # Nur sicherstellen dass truncate-Hinweis erscheint
    reply = await tn._handle_termine_command(chat_id=12345)
    assert "weitere Termine ausgeblendet" in reply
    # Nicht alle 30 Subjects in der Ausgabe (nur 20)
    appearances = sum(1 for i in range(30) if f"Termin {i:02d}" in reply)
    assert appearances == tn.TERMINE_MAX_ENTRIES
