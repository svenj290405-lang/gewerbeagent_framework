"""Tests fuer das Rueckruf-System (Voice-Tool + Telegram-Abhaken).

Deckt:
- _handle_rueckruf_anfordern: Happy-Path, Pflichtfeld-Validierung,
  Name-Default, Tenant-unbekannt, Routing-/Push-Failsafe
- Security: HTML/Telegram-Injection wird escaped; Push-Keyboard traegt
  korrektes callback_data
- _handle_rueckruf_callback: Abhaken setzt Status/Timestamps; Cross-
  Tenant-Schutz (fremde UUID kann nicht abgehakt werden); Idempotenz;
  ungueltige Daten
- _handle_rueckrufe_command: leer vs. Liste

Keine echte DB / HTTP — Sessions, Routing und Telegram werden gemockt
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
from plugins.telegram_notify import handler as tn
from core.models.rueckruf import (
    RUECKRUF_STATUS_OFFEN, RUECKRUF_STATUS_ERLEDIGT,
)


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
    push = push_mock or AsyncMock(return_value=True)
    monkeypatch.setattr(
        tn.TelegramNotifier, "send_for_employee_with_keyboard", push,
    )
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

    # Push wurde mit Inline-Keyboard + korrektem callback_data ausgeloest
    assert push.await_count == 1
    args, kwargs = push.call_args
    tenant_id_arg, msg, keyboard = args[0], args[1], args[2]
    assert tenant_id_arg == tenant.id
    cb = keyboard["inline_keyboard"][0][0]["callback_data"]
    assert cb == f"rueckruf:erledigt:{result['rueckruf_id']}"


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
    """Telegram-Push-Fehler darf die erfasste Rueckrufbitte nicht
    ruecksetzen — die Daten sind committet, Erfolg wird gemeldet."""
    tenant = _make_tenant("pilot")
    push = AsyncMock(side_effect=RuntimeError("telegram down"))
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
    _, kwargs = push.call_args
    assert kwargs["employee_id"] == emp_id


# =====================================================================
# Security: HTML/Telegram-Injection
# =====================================================================

@pytest.mark.asyncio
async def test_rueckruf_escapes_html_injection(monkeypatch):
    """Praeparierter Name/Anliegen/Telefon darf kein rohes HTML in die
    parse_mode=HTML-Telegram-Nachricht schmuggeln."""
    tenant = _make_tenant("pilot")
    captured, push = _patch_voice(monkeypatch, tenant=tenant)
    plugin = _make_plugin()
    await plugin._handle_rueckruf_anfordern(_valid_payload(
        kunde_name="<b>Hacker</b>",
        anliegen="<script>alert(1)</script>",
        kunde_telefon="<i>+49</i>",
        kunde_email="<a href=x>m@x.de</a>",
    ))
    msg = push.call_args[0][1]
    # Roh-Markup darf NICHT durchkommen
    assert "<script>" not in msg
    assert "<b>Hacker</b>" not in msg
    # Escaped-Form muss drin sein
    assert "&lt;script&gt;" in msg
    assert "&lt;b&gt;Hacker&lt;/b&gt;" in msg


# =====================================================================
# _handle_rueckruf_callback — Funktion + Security
# =====================================================================

def _cb_session_factory(rueckruf_obj):
    """Session fuer den Callback-Handler: execute() liefert den Rueckruf,
    commit() ist no-op. with_for_update() ist Teil des Statements, das die
    Fake-execute ignoriert."""
    class _S:
        async def execute(self, _stmt):
            return _FakeResult(rueckruf_obj)

        async def commit(self):
            pass

    @asynccontextmanager
    async def cm():
        yield _S()

    return cm


def _make_rueckruf_row(tenant_id, status=RUECKRUF_STATUS_OFFEN):
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        kunde_name="Frau Mueller",
        status=status,
        erledigt_at=None,
        erledigt_by_employee_id=None,
    )


def _patch_callback(monkeypatch, *, tenant, employee, rueckruf_obj):
    answer = AsyncMock()
    send = AsyncMock()
    monkeypatch.setattr(tn, "_answer_callback_query", answer)
    monkeypatch.setattr(tn, "_send_to_chat", send)
    monkeypatch.setattr(
        tn, "_get_current_employee",
        AsyncMock(return_value=(tenant, employee)),
    )
    monkeypatch.setattr(
        tn, "AsyncSessionLocal", _cb_session_factory(rueckruf_obj),
    )
    return answer, send


@pytest.mark.asyncio
async def test_callback_marks_erledigt(monkeypatch):
    tenant = _make_tenant("pilot")
    emp = SimpleNamespace(id=uuid.uuid4(), is_default=True)
    rr = _make_rueckruf_row(tenant.id)
    answer, send = _patch_callback(
        monkeypatch, tenant=tenant, employee=emp, rueckruf_obj=rr,
    )
    await tn._handle_rueckruf_callback(
        123, f"rueckruf:erledigt:{rr.id}", "cbid", "bot",
    )
    assert rr.status == RUECKRUF_STATUS_ERLEDIGT
    assert rr.erledigt_at is not None
    assert rr.erledigt_by_employee_id == emp.id
    answer.assert_awaited()


@pytest.mark.asyncio
async def test_callback_cross_tenant_rejected(monkeypatch):
    """SECURITY: ein fremder Chat darf einen Rueckruf eines ANDEREN
    Betriebs nicht per erratener UUID abhaken."""
    chat_tenant = _make_tenant("pilot")
    other_tenant_id = uuid.uuid4()
    emp = SimpleNamespace(id=uuid.uuid4(), is_default=True)
    rr = _make_rueckruf_row(other_tenant_id)  # gehoert NICHT dem Chat-Tenant
    answer, send = _patch_callback(
        monkeypatch, tenant=chat_tenant, employee=emp, rueckruf_obj=rr,
    )
    await tn._handle_rueckruf_callback(
        123, f"rueckruf:erledigt:{rr.id}", "cbid", "bot",
    )
    # Status UNVERAENDERT, Hinweis "nicht gefunden"
    assert rr.status == RUECKRUF_STATUS_OFFEN
    assert rr.erledigt_at is None
    msg = answer.call_args[0][1]
    assert "nicht gefunden" in msg.lower()


@pytest.mark.asyncio
async def test_callback_already_erledigt_idempotent(monkeypatch):
    tenant = _make_tenant("pilot")
    emp = SimpleNamespace(id=uuid.uuid4(), is_default=True)
    rr = _make_rueckruf_row(tenant.id, status=RUECKRUF_STATUS_ERLEDIGT)
    rr.erledigt_at = dt.datetime.now(dt.timezone.utc)
    answer, send = _patch_callback(
        monkeypatch, tenant=tenant, employee=emp, rueckruf_obj=rr,
    )
    await tn._handle_rueckruf_callback(
        123, f"rueckruf:erledigt:{rr.id}", "cbid", "bot",
    )
    msg = answer.call_args[0][1]
    assert "schon erledigt" in msg.lower()


@pytest.mark.asyncio
async def test_callback_invalid_uuid(monkeypatch):
    tenant = _make_tenant("pilot")
    emp = SimpleNamespace(id=uuid.uuid4(), is_default=True)
    answer, send = _patch_callback(
        monkeypatch, tenant=tenant, employee=emp, rueckruf_obj=None,
    )
    await tn._handle_rueckruf_callback(
        123, "rueckruf:erledigt:not-a-uuid", "cbid", "bot",
    )
    answer.assert_awaited()
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_callback_bad_action(monkeypatch):
    tenant = _make_tenant("pilot")
    emp = SimpleNamespace(id=uuid.uuid4(), is_default=True)
    answer, send = _patch_callback(
        monkeypatch, tenant=tenant, employee=emp, rueckruf_obj=None,
    )
    await tn._handle_rueckruf_callback(
        123, "rueckruf:loeschen:" + str(uuid.uuid4()), "cbid", "bot",
    )
    msg = answer.call_args[0][1]
    assert "ungueltig" in msg.lower()
    send.assert_not_awaited()


# =====================================================================
# _handle_rueckrufe_command
# =====================================================================

@pytest.mark.asyncio
async def test_command_empty_returns_text(monkeypatch):
    tenant = _make_tenant("pilot")
    emp = SimpleNamespace(id=uuid.uuid4(), is_default=True)
    monkeypatch.setattr(
        tn, "_get_current_employee", AsyncMock(return_value=(tenant, emp)),
    )
    monkeypatch.setattr(tn, "_load_open_rueckrufe", AsyncMock(return_value=[]))
    send = AsyncMock()
    monkeypatch.setattr(tn, "_send_to_chat", send)
    reply = await tn._handle_rueckrufe_command(123)
    assert reply is not None
    assert "keine offenen" in reply.lower()


@pytest.mark.asyncio
async def test_command_lists_with_buttons(monkeypatch):
    tenant = _make_tenant("pilot")
    emp = SimpleNamespace(id=uuid.uuid4(), is_default=True)
    rows = [
        SimpleNamespace(
            id=uuid.uuid4(), kunde_name="A", kunde_telefon="+49 1",
            anliegen="x", created_at=dt.datetime.now(dt.timezone.utc),
        ),
        SimpleNamespace(
            id=uuid.uuid4(), kunde_name="B", kunde_telefon="+49 2",
            anliegen="y", created_at=dt.datetime.now(dt.timezone.utc),
        ),
    ]
    monkeypatch.setattr(
        tn, "_get_current_employee", AsyncMock(return_value=(tenant, emp)),
    )
    monkeypatch.setattr(tn, "_load_open_rueckrufe", AsyncMock(return_value=rows))
    monkeypatch.setattr(tn, "_send_to_chat", AsyncMock())
    kb = AsyncMock()
    monkeypatch.setattr(tn, "_send_with_keyboard", kb)
    reply = await tn._handle_rueckrufe_command(123)
    assert reply is None  # Antwort lief ueber _send_with_keyboard
    assert kb.await_count == 2
    # Jeder Button traegt die richtige rueckruf-id
    sent_cbs = {
        c.args[2]["inline_keyboard"][0][0]["callback_data"]
        for c in kb.call_args_list
    }
    assert sent_cbs == {f"rueckruf:erledigt:{r.id}" for r in rows}
