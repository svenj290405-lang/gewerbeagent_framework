"""Tests fuer GoogleCalendarAdapter.append_to_description.

Deckt den nachtraeglichen Drive-Link-Eintrag ab (neuer Mail-Flow: Termin
wird VOR dem Formular gebucht, der Drive-Ordner entsteht erst beim
Formular-Eingang -> Beschreibung wird nachgepatcht). Verifiziert:
  - Zeile wird angehaengt wenn sie fehlt (bestehende Beschreibung bleibt)
  - idempotent: schon vorhandene Zeile -> kein zweiter patch

Der Google-Service wird durch einen Fake ersetzt (kein OAuth/Netz).
"""
from __future__ import annotations

import uuid

import pytest

from plugins.kalender.adapters import GoogleCalendarAdapter


class _FakeExec:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _FakeEvents:
    def __init__(self, store: dict):
        self.store = store
        self.patched = None

    def get(self, *, calendarId, eventId):  # noqa: N803
        return _FakeExec(self.store.get(eventId, {}))

    def patch(self, *, calendarId, eventId, body):  # noqa: N803
        self.patched = {"eventId": eventId, "body": body}
        self.store.setdefault(eventId, {}).update(body)
        return _FakeExec(self.store[eventId])


class _FakeService:
    def __init__(self, store: dict):
        self._events = _FakeEvents(store)

    def events(self):
        return self._events


def _adapter(store):
    a = GoogleCalendarAdapter(tenant_id=uuid.uuid4(), calendar_id="primary")
    a._service = _FakeService(store)  # _get_service gibt das durch
    return a


@pytest.mark.asyncio
async def test_append_to_description_appends_when_missing():
    store = {"ev1": {"description": "Kunde: Max\nAnliegen: Kueche"}}
    adapter = _adapter(store)

    ok = await adapter.append_to_description(
        "ev1", "Unterlagen (Drive): https://drive.google.com/x",
    )

    assert ok is True
    desc = store["ev1"]["description"]
    assert desc.startswith("Kunde: Max")  # Bestehendes bleibt erhalten
    assert "Unterlagen (Drive): https://drive.google.com/x" in desc


@pytest.mark.asyncio
async def test_append_to_description_is_idempotent():
    store = {
        "ev1": {
            "description": (
                "Kunde: Max\nUnterlagen (Drive): https://drive.google.com/x"
            )
        }
    }
    adapter = _adapter(store)

    ok = await adapter.append_to_description(
        "ev1", "Unterlagen (Drive): https://drive.google.com/x",
    )

    assert ok is True
    # Zeile war schon da -> KEIN patch
    assert adapter._service.events().patched is None


@pytest.mark.asyncio
async def test_append_to_description_empty_extra_is_noop():
    store = {"ev1": {"description": "Kunde: Max"}}
    adapter = _adapter(store)

    ok = await adapter.append_to_description("ev1", "   ")

    assert ok is True
    assert store["ev1"]["description"] == "Kunde: Max"
