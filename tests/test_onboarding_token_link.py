"""Tests fuer S13: token-basiertes Inhaber-Onboarding + Abschaltung der
ratbaren `/start <slug>`-Bindung.

Deckt:
- _handle_activate_token_start: Default-Employee (Inhaber) mit noch
  offenem Onboarding -> /onboarding-Tutorial statt /kalender_verbinden.
- _handle_start_command: bare `/start <slug>` bindet nicht mehr; bereits
  verbundener Chat bekommt freundliche Bestaetigung; `activate_<token>`
  wird weiter an den Token-Handler geleitet.
- core.onboarding.create_tenant_record: Slug-Validierung (vor DB-Zugriff).
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.telegram_notify import handler as tn_handler


class _ScalarsResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return list(self._items)

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None


@pytest.mark.asyncio
async def test_activate_owner_shows_onboarding_tutorial(monkeypatch):
    """Default-Employee (Inhaber), Onboarding noch nicht durch -> der
    Token-Pfad leitet ins /onboarding-Tutorial (nicht /kalender_verbinden)."""
    tenant_id = uuid.uuid4()
    emp = SimpleNamespace(
        id=uuid.uuid4(), tenant_id=tenant_id, slug="default",
        name="Sven Inhaber", telegram_chat_id=None, is_default=True,
    )
    tenant = SimpleNamespace(
        id=tenant_id, slug="mueller", company_name="Schreinerei Mueller",
        telegram_chat_id=None, onboarding_completed_at=None,
    )
    token_row = SimpleNamespace(employee_id=emp.id, tenant_id=tenant_id)
    monkeypatch.setattr(
        "core.models.consume_activation_token",
        AsyncMock(return_value=token_row), raising=False,
    )

    results = [
        _ScalarsResult([emp]),     # Employee-Lookup
        _ScalarsResult([tenant]),  # Tenant-Lookup
        _ScalarsResult([]),        # Stale-Lookup
    ]

    class _Session:
        async def execute(self, stmt):
            return results.pop(0) if results else _ScalarsResult([])

        async def commit(self):
            pass

        async def flush(self):
            pass

    @asynccontextmanager
    async def cm():
        yield _Session()

    monkeypatch.setattr(tn_handler, "AsyncSessionLocal", lambda: cm())

    reply = await tn_handler._handle_activate_token_start(
        "tok", 4242, {"first_name": "Sven"},
    )
    assert emp.telegram_chat_id == 4242
    assert "/onboarding" in reply
    assert "Schreinerei Mueller" in reply
    assert "/kalender_verbinden" not in reply


@pytest.mark.asyncio
async def test_bare_slug_start_does_not_bind(monkeypatch):
    """/start <slug> bindet nicht mehr (S13) — Hinweis auf den Mail-Link."""
    monkeypatch.setattr(
        tn_handler, "_get_tenant_by_chat", AsyncMock(return_value=None),
    )
    reply = await tn_handler._handle_start_command(
        "/start mueller", 4242, {"first_name": "Fremder"},
    )
    assert "activate_" in reply
    assert "nicht mehr" in reply.lower()


@pytest.mark.asyncio
async def test_bare_slug_start_already_connected(monkeypatch):
    """Bereits verbundener Chat tippt alten Slug-Link -> Bestaetigung."""
    tenant = SimpleNamespace(company_name="Schreinerei Mueller")
    monkeypatch.setattr(
        tn_handler, "_get_tenant_by_chat", AsyncMock(return_value=tenant),
    )
    reply = await tn_handler._handle_start_command(
        "/start mueller", 4242, {"first_name": "Sven"},
    )
    assert "bereits" in reply.lower()
    assert "Schreinerei Mueller" in reply


@pytest.mark.asyncio
async def test_activate_payload_routes_to_token_handler(monkeypatch):
    """/start activate_<token> geht an den Token-Handler, nicht den Slug-Pfad."""
    called = {}

    async def fake_activate(token_str, chat_id, from_data):
        called["token"] = token_str
        return "OK-TOKEN"

    monkeypatch.setattr(tn_handler, "_handle_activate_token_start", fake_activate)
    reply = await tn_handler._handle_start_command(
        "/start activate_abc123", 4242, {"first_name": "X"},
    )
    assert reply == "OK-TOKEN"
    assert called["token"] == "abc123"


# =====================================================================
# Kurzer Aktivierungs-Code (Onboarding per Suche)
# =====================================================================

def test_short_code_helpers():
    from core.models.employee_activation_token import (
        _generate_short_code, _SHORT_CODE_ALPHABET, _SHORT_CODE_LEN,
        normalize_short_code, format_short_code,
    )
    code = _generate_short_code()
    assert len(code) == _SHORT_CODE_LEN
    assert all(c in _SHORT_CODE_ALPHABET for c in code)
    # normalize: Grossschrift, Bindestrich/Leerzeichen weg
    assert normalize_short_code(" k7p4-9x2m ") == "K7P49X2M"
    # format: XXXX-XXXX
    assert format_short_code("K7P49X2M") == "K7P4-9X2M"


@pytest.mark.asyncio
async def test_consume_activation_code_bad_format_returns_none():
    """Falsche Laenge -> None, ohne DB-Zugriff (Vor-Validierung)."""
    from core.models import consume_activation_code
    assert await consume_activation_code("abc") is None
    assert await consume_activation_code("") is None


@pytest.mark.asyncio
async def test_bare_start_unbound_prompts_for_code(monkeypatch):
    """/start ohne Payload + unverbundener Chat -> nach Code fragen + State."""
    monkeypatch.setattr(
        tn_handler, "_get_tenant_by_chat", AsyncMock(return_value=None),
    )
    saved = {}

    async def fake_save(chat_id, key, data=None):
        saved["key"] = key
        saved["data"] = data

    monkeypatch.setattr(tn_handler, "_save_state", fake_save)
    reply = await tn_handler._handle_start_command("/start", 4242, {})
    assert "Aktivierungs-Code" in reply
    assert saved["key"] == tn_handler.STATE_AWAIT_ACTIVATION_CODE


@pytest.mark.asyncio
async def test_activation_code_input_valid_binds(monkeypatch):
    """Gueltiger Code -> State weg + Bindung ueber _bind_employee_to_chat."""
    emp_id = uuid.uuid4()
    monkeypatch.setattr(
        "core.models.consume_activation_code",
        AsyncMock(return_value=SimpleNamespace(employee_id=emp_id)),
        raising=False,
    )
    monkeypatch.setattr(tn_handler, "_clear_state", AsyncMock())
    bind = AsyncMock(return_value="BOUND-OK")
    monkeypatch.setattr(tn_handler, "_bind_employee_to_chat", bind)
    reply = await tn_handler._handle_activation_code_input(4242, "K7P4-9X2M")
    assert reply == "BOUND-OK"
    assert bind.await_args.args[0] == emp_id


@pytest.mark.asyncio
async def test_activation_code_input_invalid_counts_and_locks(monkeypatch):
    """Falscher Code -> Fehlversuch hochzaehlen; nach 5 -> Sperre."""
    monkeypatch.setattr(
        "core.models.consume_activation_code",
        AsyncMock(return_value=None), raising=False,
    )
    monkeypatch.setattr(tn_handler, "_save_state", AsyncMock())
    monkeypatch.setattr(tn_handler, "_clear_state", AsyncMock())

    # 1. Fehlversuch (attempts war 0)
    monkeypatch.setattr(
        tn_handler, "_load_state",
        AsyncMock(return_value=SimpleNamespace(state_data={"attempts": 0})),
    )
    reply = await tn_handler._handle_activation_code_input(4242, "WRONGXXX")
    assert "stimmt nicht" in reply.lower()

    # 5. Fehlversuch (attempts war 4 -> 5 == MAX) -> Sperre
    monkeypatch.setattr(
        tn_handler, "_load_state",
        AsyncMock(return_value=SimpleNamespace(state_data={"attempts": 4})),
    )
    reply = await tn_handler._handle_activation_code_input(4242, "WRONGXXX")
    assert "Zu viele" in reply


@pytest.mark.asyncio
async def test_create_tenant_record_rejects_bad_slug():
    """Slug-Validierung greift VOR jedem DB-Zugriff."""
    from core.onboarding import OnboardingError, create_tenant_record
    with pytest.raises(OnboardingError):
        await create_tenant_record(
            slug="x", name="A", email="a@b.de", contact="C",
        )  # zu kurz
    with pytest.raises(OnboardingError):
        await create_tenant_record(
            slug="hat leerzeichen", name="A", email="a@b.de", contact="C",
        )  # ungueltiges Zeichen
    with pytest.raises(OnboardingError):
        await create_tenant_record(
            slug="_global", name="A", email="a@b.de", contact="C",
        )  # reserviert
