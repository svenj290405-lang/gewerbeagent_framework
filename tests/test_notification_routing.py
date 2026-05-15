"""Tests fuer Teil B der Multi-Mitarbeiter-Erweiterung:
Notification-Routing auf Employee-Ebene.

Deckt:
- TelegramNotifier.resolve_employee_push_target: Employee-Chat,
  Aktivierungs-Praefix-Fallback, Default-Fallback, Legacy-Fallback,
  kein-Tool-konfiguriert
- TelegramNotifier.send_for_employee: ruft _send_raw mit Praefix +
  Text auf, schluckt Exceptions silent
- _anliegen_text_from_antworten: aggregiert nur strings
- _notify_move: pusht an sick_emp und new_emp mit richtigen Labels
"""
from __future__ import annotations

import datetime as dt
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.telegram_notify import handler as tn_handler
from core.integrations import absence_redistribution as ar
from core.integrations import anfrage_telegram as at


# =====================================================================
# Shared Test-Doubles (parallel zu tests/test_voice_routing.py)
# =====================================================================


class _FakeResult:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


def _make_session_factory(results):
    """AsyncSessionLocal-Ersatz mit gemeinsamer FIFO-Queue ueber alle
    `async with`-Bloecke. Jeder execute().scalar_one_or_none() pop't
    den naechsten Wert.
    """
    shared = list(results)

    class _SharedSession:
        async def execute(self, stmt):
            obj = shared.pop(0) if shared else None
            return _FakeResult(obj)

    @asynccontextmanager
    async def cm():
        yield _SharedSession()

    def factory():
        return cm()

    return factory


def _make_tool_config(*, bot_token="bot-123", chat_id_legacy="", enabled=True):
    return SimpleNamespace(
        enabled=enabled,
        config={"bot_token": bot_token, "chat_id": chat_id_legacy},
    )


def _make_employee(
    *, slug="emp", name=None, is_default=False, telegram_chat_id=None,
    tenant_id=None,
):
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id or uuid.uuid4(),
        slug=slug,
        name=name or slug.capitalize(),
        is_default=is_default,
        telegram_chat_id=telegram_chat_id,
    )


# =====================================================================
# resolve_employee_push_target
# =====================================================================


@pytest.mark.asyncio
async def test_resolve_target_employee_with_chat_id(monkeypatch):
    """Mitarbeiter hat eigene chat_id → diese wird genutzt, kein Praefix."""
    tenant_id = uuid.uuid4()
    emp = _make_employee(slug="daniel", telegram_chat_id=111222, tenant_id=tenant_id)
    tc = _make_tool_config(bot_token="bot-xxx")

    monkeypatch.setattr(
        tn_handler, "AsyncSessionLocal",
        _make_session_factory([tc, emp]),
    )
    bot_token, chat_id, prefix = await tn_handler.TelegramNotifier.resolve_employee_push_target(
        tenant_id, emp.id,
    )
    assert bot_token == "bot-xxx"
    assert chat_id == "111222"
    assert prefix == ""


@pytest.mark.asyncio
async def test_resolve_target_employee_without_chat_falls_back_with_prefix(monkeypatch):
    """Mitarbeiter ohne chat_id → Default-Chat mit '[unzugewiesen fuer NAME]'."""
    tenant_id = uuid.uuid4()
    emp = _make_employee(
        slug="daniel", name="Daniel Mueller",
        telegram_chat_id=None, tenant_id=tenant_id,
    )
    default_emp = _make_employee(
        slug="inhaber", name="Inhaber", is_default=True,
        telegram_chat_id=999, tenant_id=tenant_id,
    )
    tc = _make_tool_config(bot_token="bot-xxx")

    monkeypatch.setattr(
        tn_handler, "AsyncSessionLocal",
        _make_session_factory([tc, emp, default_emp]),
    )
    bot_token, chat_id, prefix = await tn_handler.TelegramNotifier.resolve_employee_push_target(
        tenant_id, emp.id,
    )
    assert bot_token == "bot-xxx"
    assert chat_id == "999"
    assert prefix == "[unzugewiesen fuer Daniel Mueller]\n\n"


@pytest.mark.asyncio
async def test_resolve_target_employee_label_override(monkeypatch):
    """Explicit employee_label hat Vorrang ueber emp.name im Praefix."""
    tenant_id = uuid.uuid4()
    emp = _make_employee(name="Original Name", telegram_chat_id=None, tenant_id=tenant_id)
    default_emp = _make_employee(
        is_default=True, telegram_chat_id=999, tenant_id=tenant_id,
    )
    tc = _make_tool_config()

    monkeypatch.setattr(
        tn_handler, "AsyncSessionLocal",
        _make_session_factory([tc, emp, default_emp]),
    )
    _, _, prefix = await tn_handler.TelegramNotifier.resolve_employee_push_target(
        tenant_id, emp.id, employee_label="Custom Label",
    )
    assert "[unzugewiesen fuer Custom Label]" in prefix


@pytest.mark.asyncio
async def test_resolve_target_no_employee_id_uses_default(monkeypatch):
    """employee_id=None → Default-Employee chat_id, kein Praefix."""
    tenant_id = uuid.uuid4()
    default_emp = _make_employee(is_default=True, telegram_chat_id=555, tenant_id=tenant_id)
    tc = _make_tool_config()

    monkeypatch.setattr(
        tn_handler, "AsyncSessionLocal",
        _make_session_factory([tc, default_emp]),
    )
    bot_token, chat_id, prefix = await tn_handler.TelegramNotifier.resolve_employee_push_target(
        tenant_id, None,
    )
    assert chat_id == "555"
    assert prefix == ""


@pytest.mark.asyncio
async def test_resolve_target_legacy_chat_id_when_no_employees(monkeypatch):
    """Kein Employee mit chat_id → Legacy chat_id aus tool_configs."""
    tenant_id = uuid.uuid4()
    tc = _make_tool_config(chat_id_legacy="LEG-42")

    monkeypatch.setattr(
        tn_handler, "AsyncSessionLocal",
        _make_session_factory([tc, None]),  # kein default_emp gefunden
    )
    bot_token, chat_id, prefix = await tn_handler.TelegramNotifier.resolve_employee_push_target(
        tenant_id, None,
    )
    assert chat_id == "LEG-42"
    assert prefix == ""


@pytest.mark.asyncio
async def test_resolve_target_no_tool_config_returns_none(monkeypatch):
    """telegram_notify nicht aktiviert → (None, None, '')."""
    tenant_id = uuid.uuid4()
    monkeypatch.setattr(
        tn_handler, "AsyncSessionLocal",
        _make_session_factory([None]),  # kein ToolConfig
    )
    out = await tn_handler.TelegramNotifier.resolve_employee_push_target(
        tenant_id, None,
    )
    assert out == (None, None, "")


@pytest.mark.asyncio
async def test_resolve_target_disabled_tool_config_returns_none(monkeypatch):
    """ToolConfig disabled → (None, None, '')."""
    tc = _make_tool_config(enabled=False)
    monkeypatch.setattr(
        tn_handler, "AsyncSessionLocal",
        _make_session_factory([tc]),
    )
    out = await tn_handler.TelegramNotifier.resolve_employee_push_target(
        uuid.uuid4(), None,
    )
    assert out == (None, None, "")


@pytest.mark.asyncio
async def test_resolve_target_missing_bot_token_returns_none(monkeypatch):
    """ToolConfig enabled aber bot_token leer → (None, None, '')."""
    tc = _make_tool_config(bot_token="")
    monkeypatch.setattr(
        tn_handler, "AsyncSessionLocal",
        _make_session_factory([tc]),
    )
    out = await tn_handler.TelegramNotifier.resolve_employee_push_target(
        uuid.uuid4(), None,
    )
    assert out == (None, None, "")


# =====================================================================
# send_for_employee
# =====================================================================


@pytest.mark.asyncio
async def test_send_for_employee_appends_prefix(monkeypatch):
    """Wenn Praefix gesetzt: _send_raw bekommt prefix+text."""
    sent = {}

    async def fake_send_raw(bot_token, chat_id, text):
        sent.update(bot_token=bot_token, chat_id=chat_id, text=text)
        return True

    async def fake_resolve(tenant_id, employee_id, *, employee_label=None):
        return ("bot-x", "555", "[unzugewiesen fuer Max]\n\n")

    monkeypatch.setattr(
        tn_handler.TelegramNotifier, "_send_raw", staticmethod(fake_send_raw),
    )
    monkeypatch.setattr(
        tn_handler.TelegramNotifier, "resolve_employee_push_target",
        staticmethod(fake_resolve),
    )
    ok = await tn_handler.TelegramNotifier.send_for_employee(
        uuid.uuid4(), "Hallo", employee_id=uuid.uuid4(),
    )
    assert ok is True
    assert sent["text"] == "[unzugewiesen fuer Max]\n\nHallo"


@pytest.mark.asyncio
async def test_send_for_employee_no_prefix(monkeypatch):
    sent = {}

    async def fake_send_raw(bot_token, chat_id, text):
        sent["text"] = text
        return True

    async def fake_resolve(tenant_id, employee_id, *, employee_label=None):
        return ("bot-x", "555", "")

    monkeypatch.setattr(
        tn_handler.TelegramNotifier, "_send_raw", staticmethod(fake_send_raw),
    )
    monkeypatch.setattr(
        tn_handler.TelegramNotifier, "resolve_employee_push_target",
        staticmethod(fake_resolve),
    )
    ok = await tn_handler.TelegramNotifier.send_for_employee(
        uuid.uuid4(), "Hallo", employee_id=None,
    )
    assert ok is True
    assert sent["text"] == "Hallo"


@pytest.mark.asyncio
async def test_send_for_employee_returns_false_when_no_target(monkeypatch):
    """Kein Target → False, _send_raw wird nicht gerufen."""
    raw_calls = []

    async def fake_send_raw(*a, **kw):
        raw_calls.append(a)
        return True

    async def fake_resolve(*a, **kw):
        return (None, None, "")

    monkeypatch.setattr(
        tn_handler.TelegramNotifier, "_send_raw", staticmethod(fake_send_raw),
    )
    monkeypatch.setattr(
        tn_handler.TelegramNotifier, "resolve_employee_push_target",
        staticmethod(fake_resolve),
    )
    ok = await tn_handler.TelegramNotifier.send_for_employee(
        uuid.uuid4(), "x", employee_id=None,
    )
    assert ok is False
    assert raw_calls == []


@pytest.mark.asyncio
async def test_send_for_employee_swallows_exceptions(monkeypatch):
    """Exception im Resolver → False statt Crash (silent-fail-Kontrakt)."""
    async def boom(*a, **kw):
        raise RuntimeError("DB down")

    monkeypatch.setattr(
        tn_handler.TelegramNotifier, "resolve_employee_push_target",
        staticmethod(boom),
    )
    ok = await tn_handler.TelegramNotifier.send_for_employee(
        uuid.uuid4(), "x", employee_id=None,
    )
    assert ok is False


# =====================================================================
# anfrage_telegram._anliegen_text_from_antworten
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
    """sick_emp + new_emp bekommen je einen Push mit den richtigen Labels."""
    calls = []

    async def fake_send_for_employee(tenant_id, text, *, employee_id, employee_label=None):
        calls.append({
            "tenant_id": tenant_id, "text": text,
            "employee_id": employee_id, "employee_label": employee_label,
        })
        return True

    monkeypatch.setattr(
        tn_handler.TelegramNotifier, "send_for_employee",
        staticmethod(fake_send_for_employee),
    )

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

    assert "umgehaengt" in sick_call["text"]
    assert "Max Schmidt" in sick_call["text"]  # uebernimmt
    assert sick_call["employee_label"] == "Daniel Mueller"

    assert "uebernimmst" in new_call["text"]
    assert "Daniel Mueller" in new_call["text"]  # von
    assert new_call["employee_label"] == "Max Schmidt"


@pytest.mark.asyncio
async def test_notify_move_silent_fail(monkeypatch):
    """Push-Exception darf nicht hochbubbeln — Umverteilung soll weiterlaufen."""
    async def boom(*a, **kw):
        raise RuntimeError("Telegram down")

    monkeypatch.setattr(
        tn_handler.TelegramNotifier, "send_for_employee", staticmethod(boom),
    )
    tenant = SimpleNamespace(id=uuid.uuid4(), slug="demo")
    sick = SimpleNamespace(id=uuid.uuid4(), slug="d", name="D")
    new = SimpleNamespace(id=uuid.uuid4(), slug="m", name="M")
    # Sollte NICHT raisen
    await ar._notify_move(
        tenant, sick, new, {"subject": "x"}, dt.datetime(2026, 5, 20, 9, 0),
    )
