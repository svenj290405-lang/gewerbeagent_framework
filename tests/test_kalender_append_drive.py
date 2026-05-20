"""Tests fuer GoogleCalendarAdapter.attach_drive_link.

Deckt den nachtraeglichen Drive-Link-Eintrag ab (neuer Flow: Termin wird
VOR dem Formular gebucht, der Drive-Ordner entsteht erst beim Formular-
Eingang -> Beschreibung wird nachgepatcht). Verifiziert:
  - Link wird VOR dem GA-Footer eingefuegt (bei den Kundendaten, in der
    Kurzvorschau sichtbar) — nicht ans Ende
  - bestehende Beschreibung bleibt erhalten
  - idempotent: schon vorhandener Link -> kein zweiter patch
  - ohne Footer -> ans Ende angehaengt

Der Google-Service wird durch einen Fake ersetzt (kein OAuth/Netz).
"""
from __future__ import annotations

import uuid

import pytest

from plugins.kalender.adapters import GoogleCalendarAdapter

FOOTER = "Eingetragen via KI-Agent Q (Gewerbeagent Framework)"
URL = "https://drive.google.com/drive/folders/abc123"


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
async def test_attach_drive_link_inserts_before_footer():
    store = {"ev1": {"description": f"Kunde: Max\nTelefon: 0151\n\n{FOOTER}\nGA-Ref: x"}}
    adapter = _adapter(store)

    ok = await adapter.attach_drive_link("ev1", URL)

    assert ok is True
    desc = store["ev1"]["description"]
    assert f"Unterlagen (Drive): {URL}" in desc
    # vor dem Footer (also bei den Kundendaten, vorschau-sichtbar)
    assert desc.index("Unterlagen (Drive)") < desc.index(FOOTER)
    # Bestehendes bleibt erhalten
    assert "Kunde: Max" in desc and "Telefon: 0151" in desc and "GA-Ref: x" in desc


@pytest.mark.asyncio
async def test_attach_drive_link_is_idempotent():
    store = {"ev1": {"description": f"Kunde: Max\nUnterlagen (Drive): {URL}\n\n{FOOTER}"}}
    adapter = _adapter(store)

    ok = await adapter.attach_drive_link("ev1", URL)

    assert ok is True
    assert adapter._service.events().patched is None  # schon dran -> kein patch


@pytest.mark.asyncio
async def test_attach_drive_link_without_footer_appends():
    store = {"ev1": {"description": "Kunde: Max"}}
    adapter = _adapter(store)

    ok = await adapter.attach_drive_link("ev1", URL)

    assert ok is True
    desc = store["ev1"]["description"]
    assert desc.startswith("Kunde: Max")
    assert f"Unterlagen (Drive): {URL}" in desc


@pytest.mark.asyncio
async def test_attach_drive_link_empty_url_is_noop():
    store = {"ev1": {"description": "Kunde: Max"}}
    adapter = _adapter(store)

    ok = await adapter.attach_drive_link("ev1", "   ")

    assert ok is True
    assert store["ev1"]["description"] == "Kunde: Max"
