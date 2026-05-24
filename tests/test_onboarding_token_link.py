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
