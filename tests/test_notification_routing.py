"""Tests fuer das Notification-Routing auf Employee-Ebene.

Deckt:
- _anliegen_text_from_antworten: aggregiert nur strings
- _notify_move: pusht an sick_emp und new_emp

Die frueheren Tests der Telegram-Chat-Aufloesung
(Chat-Aufloesung des alten Bots)
sind mit dem Bot entfallen (2026-08-21) — einziger Kanal ist Web-Push
ueber core/integrations/notify.py.
"""
from __future__ import annotations

import datetime as dt
import uuid
from types import SimpleNamespace

import pytest

from core.integrations import absence_redistribution as ar
from core.integrations import anfrage_eingang as at


# =====================================================================
# anfrage_eingang._anliegen_text_from_antworten
# =====================================================================


def test_anliegen_text_only_strings():
    out = at._anliegen_text_from_antworten({
        "anliegen": "Heizung defekt",
        "datei": [{"filename": "foto.jpg"}],
        "anzahl_raeume": 3,  # int → ignored
        "kommentar": "  Mehr Details  ",
        "leer": "",
    })
    assert "Heizung defekt" in out
    assert "Mehr Details" in out
    assert "foto.jpg" not in out


def test_anliegen_text_empty():
    assert at._anliegen_text_from_antworten({}) == ""
    assert at._anliegen_text_from_antworten(None) == ""


# =====================================================================
# absence_redistribution._notify_move
# =====================================================================


@pytest.mark.asyncio
async def test_notify_move_pushes_both_employees(monkeypatch):
    """sick_emp + new_emp bekommen je einen Push — ohne Kunden-PII."""
    calls = []

    async def fake_notify_employee(tenant_id, employee_id, **kw):
        calls.append({"tenant_id": tenant_id, "employee_id": employee_id, **kw})
        return 1

    import core.integrations.notify as notify_mod
    monkeypatch.setattr(notify_mod, "notify_employee", fake_notify_employee)

    tenant = SimpleNamespace(id=uuid.uuid4(), slug="demo")
    sick = SimpleNamespace(
        id=uuid.uuid4(), slug="daniel", name="Daniel Mueller",
        calendar_provider="microsoft",
    )
    new = SimpleNamespace(
        id=uuid.uuid4(), slug="max", name="Max Schmidt",
        calendar_provider="google",
    )
    event = {"subject": "Heizungswartung bei Mueller", "event_id": "e1"}
    start_dt = dt.datetime(2026, 5, 20, 14, 0)

    await ar._notify_move(tenant, sick, new, event, start_dt)

    assert len(calls) == 2
    sick_call = next(c for c in calls if c["employee_id"] == sick.id)
    new_call = next(c for c in calls if c["employee_id"] == new.id)

    assert sick_call["title"] == "Termin umgehängt"
    assert new_call["title"] == "Du übernimmst einen Termin"
    # Kunden-/Mitarbeiternamen duerfen nicht in den Push (FCM/APNs).
    for c in calls:
        blob = f"{c['title']} {c['body']}"
        assert "Mueller" not in blob
        assert "Max Schmidt" not in blob


@pytest.mark.asyncio
async def test_notify_move_silent_fail(monkeypatch):
    """Push-Exception darf nicht hochbubbeln — Umverteilung soll weiterlaufen."""
    async def boom(*a, **kw):
        raise RuntimeError("Push down")

    import core.integrations.notify as notify_mod
    monkeypatch.setattr(notify_mod, "notify_employee", boom)

    tenant = SimpleNamespace(id=uuid.uuid4(), slug="demo")
    sick = SimpleNamespace(id=uuid.uuid4(), slug="d", name="D")
    new = SimpleNamespace(id=uuid.uuid4(), slug="m", name="M")
    # Sollte NICHT raisen
    await ar._notify_move(
        tenant, sick, new, {"subject": "x"}, dt.datetime(2026, 5, 20, 9, 0),
    )
