"""Tests fuer die /archiv-Architektur (refactor 2026-05-17).

Deckt:
- /archiv-Liste: nicht verbunden / leer / mit Eintraegen (mit Klick-Link)
- /archiv <text> intelligent: 0 / 1 / N Treffer
- Disambiguation-State-Handler: Choice + NewConfirm
- Soft-Deprecation der /drive*-Befehle
"""
from __future__ import annotations

import datetime as dt
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.telegram_notify import handler as tn


# =====================================================================
# Doubles
# =====================================================================

def _make_employee():
    return SimpleNamespace(
        id=uuid.uuid4(), slug="emp",
        is_default=True, is_active=True,
        calendar_provider="google",
    )


def _make_tenant():
    return SimpleNamespace(id=uuid.uuid4(), slug="demo")


def _make_drive_folder(*, kunde_name, count=3, url=None,
                      last=None):
    return SimpleNamespace(
        kunde_name=kunde_name,
        drive_folder_url=url or f"https://drive.example/{kunde_name.lower()}",
        upload_count=count,
        last_upload_at=last,
    )


def _patch_drive_connected(monkeypatch, *, connected=True):
    """Mockt Token-Lookup + is_drive_configured konsistent."""
    import core.security.oauth_token_lookup as otl
    import core.integrations.google_drive as gd
    monkeypatch.setattr(otl, "find_oauth_token",
                        AsyncMock(return_value=object() if connected else None))
    monkeypatch.setattr(gd, "is_drive_configured",
                        lambda tok: bool(tok))


def _patch_get_employee(monkeypatch, tenant, emp):
    monkeypatch.setattr(tn, "_get_current_employee",
                        AsyncMock(return_value=(tenant, emp)))


def _patch_list_folders(monkeypatch, folders):
    import core.integrations.google_drive as gd
    monkeypatch.setattr(gd, "list_tenant_kunde_drives",
                        AsyncMock(return_value=folders))


def _patch_save_state(monkeypatch):
    """Captures _save_state-Aufrufe."""
    saved = []

    async def fake_save(chat_id, key, data=None):
        saved.append({"chat_id": chat_id, "key": key, "data": data})

    async def fake_clear(chat_id):
        saved.append({"chat_id": chat_id, "key": None, "data": None})

    monkeypatch.setattr(tn, "_save_state", fake_save)
    monkeypatch.setattr(tn, "_clear_state", fake_clear)
    return saved


# =====================================================================
# /archiv (Liste)
# =====================================================================

@pytest.mark.asyncio
async def test_archiv_list_not_connected(monkeypatch):
    tenant = _make_tenant()
    emp = _make_employee()
    _patch_get_employee(monkeypatch, tenant, emp)
    _patch_drive_connected(monkeypatch, connected=False)
    reply = await tn._handle_archiv_list_command(chat_id=1)
    assert "nicht verbunden" in reply.lower()
    assert "/archiv_verbinden" in reply


@pytest.mark.asyncio
async def test_archiv_list_empty(monkeypatch):
    tenant = _make_tenant()
    emp = _make_employee()
    _patch_get_employee(monkeypatch, tenant, emp)
    _patch_drive_connected(monkeypatch, connected=True)
    _patch_list_folders(monkeypatch, [])
    reply = await tn._handle_archiv_list_command(chat_id=1)
    assert "Noch keine Kunden" in reply
    assert "/archiv &lt;Kundenname&gt;" in reply


@pytest.mark.asyncio
async def test_archiv_list_with_links(monkeypatch):
    tenant = _make_tenant()
    emp = _make_employee()
    _patch_get_employee(monkeypatch, tenant, emp)
    _patch_drive_connected(monkeypatch, connected=True)
    f1 = _make_drive_folder(
        kunde_name="Mueller", count=12,
        url="https://drive.example/mueller",
        last=dt.datetime(2026, 5, 15, 12, 0),
    )
    f2 = _make_drive_folder(
        kunde_name="Schulze", count=3,
        url="https://drive.example/schulze",
        last=dt.datetime(2026, 5, 14, 9, 0),
    )
    _patch_list_folders(monkeypatch, [f1, f2])
    reply = await tn._handle_archiv_list_command(chat_id=1)
    # Beide Namen sichtbar
    assert "Mueller" in reply
    assert "Schulze" in reply
    # Klick-Links als anchor
    assert '<a href="https://drive.example/mueller">' in reply
    assert '<a href="https://drive.example/schulze">' in reply
    # Stats
    assert "2 Ordner" in reply
    assert "15 Dateien" in reply  # 12+3 total


# =====================================================================
# /archiv <text> intelligent
# =====================================================================

@pytest.mark.asyncio
async def test_archiv_smart_zero_matches_asks_to_create(monkeypatch):
    tenant = _make_tenant()
    emp = _make_employee()
    _patch_get_employee(monkeypatch, tenant, emp)
    _patch_drive_connected(monkeypatch, connected=True)
    _patch_list_folders(monkeypatch, [])  # nothing existing
    saved = _patch_save_state(monkeypatch)

    reply = await tn._handle_archiv_smart_command(chat_id=1, args="Neukunde Mueller")
    assert "Kein Ordner gefunden" in reply
    assert "neu anlegen" in reply.lower()
    assert "ja" in reply.lower() and "nein" in reply.lower()
    # State NEW_CONFIRM gesetzt mit kunde_name
    assert saved[-1]["key"] == tn.STATE_ARCHIV_AWAIT_NEW_CONFIRM
    assert saved[-1]["data"]["kunde_name"] == "Neukunde Mueller"


@pytest.mark.asyncio
async def test_archiv_smart_single_match_starts_wizard(monkeypatch):
    tenant = _make_tenant()
    emp = _make_employee()
    _patch_get_employee(monkeypatch, tenant, emp)
    _patch_drive_connected(monkeypatch, connected=True)
    f = _make_drive_folder(kunde_name="Mueller Bauplanung")
    _patch_list_folders(monkeypatch, [f])
    saved = _patch_save_state(monkeypatch)

    reply = await tn._handle_archiv_smart_command(chat_id=1, args="Mueller")
    assert "Mueller Bauplanung" in reply
    assert "/fertig" in reply
    # State WAITING_FILES direkt gesetzt
    assert saved[-1]["key"] == tn.STATE_ARCHIV_WAITING_FILES
    assert saved[-1]["data"]["kunde_name"] == "Mueller Bauplanung"
    assert saved[-1]["data"]["uploaded"] == 0


@pytest.mark.asyncio
async def test_archiv_smart_multi_match_offers_choice(monkeypatch):
    tenant = _make_tenant()
    emp = _make_employee()
    _patch_get_employee(monkeypatch, tenant, emp)
    _patch_drive_connected(monkeypatch, connected=True)
    f1 = _make_drive_folder(kunde_name="Mueller Bau")
    f2 = _make_drive_folder(kunde_name="Mueller Wohnen")
    f3 = _make_drive_folder(kunde_name="Anna Mueller")
    _patch_list_folders(monkeypatch, [f1, f2, f3])
    saved = _patch_save_state(monkeypatch)

    reply = await tn._handle_archiv_smart_command(chat_id=1, args="Mueller")
    assert "3 Treffer" in reply
    assert "Mueller Bau" in reply
    assert "Mueller Wohnen" in reply
    assert "Anna Mueller" in reply
    # Nummerierte Optionen 1-3
    assert "1)" in reply and "2)" in reply and "3)" in reply
    # State AWAIT_CHOICE gesetzt mit matches-Liste
    assert saved[-1]["key"] == tn.STATE_ARCHIV_AWAIT_CHOICE
    matches = saved[-1]["data"]["matches"]
    assert len(matches) == 3
    assert {m["kunde_name"] for m in matches} == {
        "Mueller Bau", "Mueller Wohnen", "Anna Mueller",
    }


@pytest.mark.asyncio
async def test_archiv_smart_too_short_rejected(monkeypatch):
    tenant = _make_tenant()
    emp = _make_employee()
    _patch_get_employee(monkeypatch, tenant, emp)
    reply = await tn._handle_archiv_smart_command(chat_id=1, args="A")
    assert "Kunden-Namen mitgeben" in reply or "name" in reply.lower()


@pytest.mark.asyncio
async def test_archiv_smart_not_connected_hints(monkeypatch):
    tenant = _make_tenant()
    emp = _make_employee()
    _patch_get_employee(monkeypatch, tenant, emp)
    _patch_drive_connected(monkeypatch, connected=False)
    reply = await tn._handle_archiv_smart_command(chat_id=1, args="Mueller")
    assert "/archiv_verbinden" in reply


# =====================================================================
# Disambiguation-Handler
# =====================================================================

@pytest.mark.asyncio
async def test_archiv_choice_invalid_number(monkeypatch):
    saved = _patch_save_state(monkeypatch)
    state_data = {
        "matches": [{"kunde_name": "A"}, {"kunde_name": "B"}],
        "tenant_id": str(uuid.uuid4()),
        "employee_id": str(uuid.uuid4()),
    }
    reply = await tn._handle_archiv_choice_input(1, "abc", state_data)
    assert "Nummer" in reply
    # State darf nicht geaendert sein
    assert all(s["key"] != tn.STATE_ARCHIV_WAITING_FILES for s in saved)


@pytest.mark.asyncio
async def test_archiv_choice_picks_match_starts_wizard(monkeypatch):
    saved = _patch_save_state(monkeypatch)
    state_data = {
        "matches": [
            {"kunde_name": "Mueller Bau"},
            {"kunde_name": "Mueller Wohnen"},
        ],
        "tenant_id": str(uuid.uuid4()),
        "employee_id": str(uuid.uuid4()),
    }
    reply = await tn._handle_archiv_choice_input(1, "2", state_data)
    assert "Mueller Wohnen" in reply
    assert "/fertig" in reply
    # Wizard-State wurde gesetzt mit dem richtigen Namen
    wizard = [s for s in saved if s["key"] == tn.STATE_ARCHIV_WAITING_FILES]
    assert len(wizard) == 1
    assert wizard[0]["data"]["kunde_name"] == "Mueller Wohnen"


@pytest.mark.asyncio
async def test_archiv_new_confirm_ja_starts_wizard(monkeypatch):
    saved = _patch_save_state(monkeypatch)
    state_data = {
        "kunde_name": "Neukunde XY",
        "tenant_id": str(uuid.uuid4()),
        "employee_id": str(uuid.uuid4()),
    }
    reply = await tn._handle_archiv_new_confirm_input(1, "ja", state_data)
    assert "Neukunde XY" in reply
    wizard = [s for s in saved if s["key"] == tn.STATE_ARCHIV_WAITING_FILES]
    assert len(wizard) == 1
    assert wizard[0]["data"]["kunde_name"] == "Neukunde XY"


@pytest.mark.asyncio
async def test_archiv_new_confirm_nein_clears(monkeypatch):
    saved = _patch_save_state(monkeypatch)
    state_data = {"kunde_name": "X", "tenant_id": str(uuid.uuid4())}
    reply = await tn._handle_archiv_new_confirm_input(1, "nein", state_data)
    assert "Abgebrochen" in reply
    cleared = [s for s in saved if s["key"] is None]
    assert len(cleared) == 1


@pytest.mark.asyncio
async def test_archiv_new_confirm_invalid_input(monkeypatch):
    reply = await tn._handle_archiv_new_confirm_input(
        1, "blubb", {"kunde_name": "X"},
    )
    assert "ja" in reply.lower() and "nein" in reply.lower()


# =====================================================================
# Soft-Deprecation /drive*
# =====================================================================

@pytest.mark.asyncio
async def test_deprecated_drive_verbinden_routes_to_new():
    reply = await tn._handle_deprecated_drive_command("/drive_verbinden")
    assert "/archiv_verbinden" in reply
    assert "/drive_verbinden" in reply  # zeigt was alt war
    assert "umbenannt" in reply.lower() or "heisst jetzt" in reply.lower()


@pytest.mark.asyncio
async def test_deprecated_drive_status_routes_to_new():
    reply = await tn._handle_deprecated_drive_command("/drive_status")
    assert "/archiv_status" in reply


@pytest.mark.asyncio
async def test_deprecated_drive_with_args_constructs_new_command():
    reply = await tn._handle_deprecated_drive_command("/drive", "Mueller")
    assert "/archiv" in reply
    assert "Mueller" in reply
