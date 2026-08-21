"""Tests fuer das Rueckruf-System (Voice-Tool).

Deckt:
- _handle_rueckruf_anfordern: Happy-Path, Pflichtfeld-Validierung,
  Name-Default, Tenant-unbekannt, Routing-/Push-Failsafe

Abgehakt wird in der App (/app#rueckrufe) — die frueheren Tests des
Telegram-Callbacks sind mit dem Bot entfallen (2026-08-21).

Keine echte DB / HTTP — Sessions, Routing und Push werden gemockt
(Muster wie tests/test_voice_email_resolution.py).
"""
from __future__ import annotations

import datetime as dt
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.voice_init import handler as voice_handler
from core.models.rueckruf import RUECKRUF_STATUS_OFFEN


# =====================================================================
# Test-Doubles
# =====================================================================

class _FakeResult:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


def _voice_session_factory(execute_results, captured):
    """AsyncSessionLocal-Ersatz fuer den Voice-Handler.

    Teilt eine FIFO-Queue ueber alle `async with`-Bloecke (Tenant-Load +
    Insert). `add` legt das eingefuegte Objekt in `captured` ab, `refresh`
    vergibt eine id (wie ein echter Flush).
    """
    shared = list(execute_results)

    class _S:
        async def execute(self, _stmt):
            return _FakeResult(shared.pop(0) if shared else None)

        def add(self, obj):
            captured["added"] = obj

        async def commit(self):
            captured["committed"] = True

        async def refresh(self, obj):
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()

    @asynccontextmanager
    async def cm():
        yield _S()

    return cm


def _make_tenant(slug="pilot"):
    return SimpleNamespace(id=uuid.uuid4(), slug=slug)


def _make_plugin():
    context = SimpleNamespace(tenant_id=uuid.uuid4(), config={})
    return voice_handler.Plugin(context)


def _patch_voice(monkeypatch, *, tenant, choose_result=None,
                 choose_raises=False, push_mock=None):
    """Wire _handle_rueckruf_anfordern-Abhaengigkeiten auf Stubs.
    Returns (captured, push_mock)."""
    captured: dict = {}
    execute_results = [tenant] if tenant is not None else [None]
    monkeypatch.setattr(
        voice_handler, "AsyncSessionLocal",
        _voice_session_factory(execute_results, captured),
    )
    if choose_raises:
        monkeypatch.setattr(
            voice_handler, "choose_employee",
            AsyncMock(side_effect=RuntimeError("routing kaputt")),
        )
    else:
        monkeypatch.setattr(
            voice_handler, "choose_employee",
            AsyncMock(return_value=choose_result),
        )
    push = push_mock or AsyncMock(return_value=1)
    import core.integrations.notify as notify_mod
    monkeypatch.setattr(notify_mod, "notify_employee", push)
    return captured, push


def _valid_payload(**over):
    p = {
        "kunde_name": "Frau Mueller",
        "kunde_telefon": "+49 651 12345",
        "anliegen": "Reklamation Kuechenfront",
        "tenant_slug": "pilot",
    }
    p.update(over)
    return p


# =====================================================================
# _handle_rueckruf_anfordern — Funktion
# =====================================================================

@pytest.mark.asyncio
async def test_rueckruf_happy_path_persists_and_pushes(monkeypatch):
    tenant = _make_tenant("pilot")
    captured, push = _patch_voice(monkeypatch, tenant=tenant)

    plugin = _make_plugin()
    result = await plugin._handle_rueckruf_anfordern(_valid_payload())

    assert result["success"] is True
    assert result["status"] == RUECKRUF_STATUS_OFFEN
    assert "rueckruf_id" in result

    # Persistierte Zeile
    rr = captured["added"]
    assert rr.tenant_id == tenant.id
    assert rr.kunde_name == "Frau Mueller"
    assert rr.kunde_telefon == "+49 651 12345"
    assert rr.anliegen == "Reklamation Kuechenfront"
    assert rr.status == RUECKRUF_STATUS_OFFEN
    assert captured.get("committed") is True

    # Push wurde ausgeloest — ohne Kunden-PII, mit Deeplink in die App
    assert push.await_count == 1
    args, kwargs = push.call_args
    assert args[0] == tenant.id
    assert kwargs["url"] == "/app#rueckrufe"
    blob = f"{kwargs['title']} {kwargs['body']}"
    assert "Mueller" not in blob
    assert "12345" not in blob


@pytest.mark.asyncio
async def test_rueckruf_missing_tenant_slug(monkeypatch):
    captured, push = _patch_voice(monkeypatch, tenant=_make_tenant())
    plugin = _make_plugin()
    result = await plugin._handle_rueckruf_anfordern(
        _valid_payload(tenant_slug="")
    )
    assert result["success"] is False
    assert "added" not in captured
    assert push.await_count == 0


@pytest.mark.asyncio
async def test_rueckruf_missing_telefon(monkeypatch):
    captured, push = _patch_voice(monkeypatch, tenant=_make_tenant())
    plugin = _make_plugin()
    result = await plugin._handle_rueckruf_anfordern(
        _valid_payload(kunde_telefon="")
    )
    assert result["success"] is False
    assert "added" not in captured


@pytest.mark.asyncio
async def test_rueckruf_missing_anliegen(monkeypatch):
    captured, push = _patch_voice(monkeypatch, tenant=_make_tenant())
    plugin = _make_plugin()
    result = await plugin._handle_rueckruf_anfordern(
        _valid_payload(anliegen="   ")
    )
    assert result["success"] is False
    assert "added" not in captured


@pytest.mark.asyncio
async def test_rueckruf_empty_name_defaults_to_unbekannt(monkeypatch):
    """Name ist Pflicht laut Spec, aber ein Rueckruf mit Telefon+Anliegen
    ist handlungsrelevant — leerer Name wird zu 'Unbekannt' statt Drop."""
    tenant = _make_tenant("pilot")
    captured, push = _patch_voice(monkeypatch, tenant=tenant)
    plugin = _make_plugin()
    result = await plugin._handle_rueckruf_anfordern(
        _valid_payload(kunde_name="")
    )
    assert result["success"] is True
    assert captured["added"].kunde_name == "Unbekannt"
    assert push.await_count == 1


@pytest.mark.asyncio
async def test_rueckruf_unknown_tenant(monkeypatch):
    captured, push = _patch_voice(monkeypatch, tenant=None)
    plugin = _make_plugin()
    result = await plugin._handle_rueckruf_anfordern(_valid_payload())
    assert result["success"] is False
    assert "unbekannt" in result["error"].lower()
    assert "added" not in captured
    assert push.await_count == 0


@pytest.mark.asyncio
async def test_rueckruf_routing_crash_still_persists(monkeypatch):
    """choose_employee-Fehler darf die Erfassung nicht verhindern."""
    tenant = _make_tenant("pilot")
    captured, push = _patch_voice(monkeypatch, tenant=tenant, choose_raises=True)
    plugin = _make_plugin()
    result = await plugin._handle_rueckruf_anfordern(_valid_payload())
    assert result["success"] is True
    assert captured["added"].assigned_employee_id is None
    assert push.await_count == 1


@pytest.mark.asyncio
async def test_rueckruf_push_crash_still_succeeds(monkeypatch):
    """Push-Fehler darf die erfasste Rueckrufbitte nicht ruecksetzen —
    die Daten sind committet, Erfolg wird gemeldet."""
    tenant = _make_tenant("pilot")
    push = AsyncMock(side_effect=RuntimeError("push down"))
    captured, _ = _patch_voice(monkeypatch, tenant=tenant, push_mock=push)
    plugin = _make_plugin()
    result = await plugin._handle_rueckruf_anfordern(_valid_payload())
    assert result["success"] is True
    assert captured.get("committed") is True


@pytest.mark.asyncio
async def test_rueckruf_assigned_employee_from_routing(monkeypatch):
    from core.routing.employee_router import RoutingDecision
    tenant = _make_tenant("pilot")
    emp_id = uuid.uuid4()
    decision = RoutingDecision(
        employee_id=emp_id, employee_name="Max", employee_slug="max",
        reason="skill-match", score=1.0, debug={"needed_skills": ["holz"]},
    )
    captured, push = _patch_voice(monkeypatch, tenant=tenant, choose_result=decision)
    plugin = _make_plugin()
    result = await plugin._handle_rueckruf_anfordern(_valid_payload())
    assert result["success"] is True
    assert captured["added"].assigned_employee_id == emp_id
    # Push an genau diesen Mitarbeiter geroutet
    args, _ = push.call_args
    assert args[1] == emp_id
