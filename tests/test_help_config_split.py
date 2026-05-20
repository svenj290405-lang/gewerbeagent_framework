"""Tests fuer die /help vs /config-Trennung.

/help = Daily-Sicht (Aufnahme/Briefing/Angebot/Archiv/...).
/config = Setup-Sicht (Verbindungen, Status, Werkstatt, Mitarbeiter,
          Onboarding).
"""
from __future__ import annotations

import pytest

from plugins.telegram_notify import handler as tn


# =====================================================================
# /help: nur Daily-Use
# =====================================================================

@pytest.mark.asyncio
async def test_help_contains_daily_commands():
    text = await tn._handle_help_command(chat_id=None)
    for needed in ("/aufnahme", "/briefing", "/neue_termine", "/angebot",
                   "/archiv", "/kunde", "/storno"):
        assert needed in text, f"/help missing daily command {needed}"


@pytest.mark.asyncio
async def test_help_does_not_contain_setup_commands():
    text = await tn._handle_help_command(chat_id=None)
    for forbidden in ("/lexware_setup", "/microsoft_setup",
                      "/archiv_verbinden", "/kalender_verbinden",
                      "/lexware_status", "/microsoft_status",
                      "/archiv_status", "/kalender_status",
                      "/werkstatt_status", "/werkstatt",
                      "/mitarbeiter", "/team", "/krank", "/urlaub",
                      "/zurueck", "/onboarding"):
        assert forbidden not in text, (
            f"/help unexpectedly contains {forbidden}"
        )


@pytest.mark.asyncio
async def test_help_does_not_list_fertig_outside_wizard():
    """User hat explizit verlangt: /fertig soll NUR im Wizard-Follow-up
    erwaehnt werden, nicht in der Haupt-Uebersicht."""
    text = await tn._handle_help_command(chat_id=None)
    assert "/fertig" not in text


@pytest.mark.asyncio
async def test_help_points_to_config():
    text = await tn._handle_help_command(chat_id=None)
    assert "/config" in text


# =====================================================================
# /config: nur Setup-Sicht
# =====================================================================

@pytest.mark.asyncio
async def test_config_contains_all_oauth_commands():
    text = await tn._handle_config_command(chat_id=None)
    for needed in ("/kalender_verbinden", "/archiv_verbinden",
                   "/lexware_setup", "/microsoft_setup"):
        assert needed in text, f"/config missing OAuth command {needed}"


@pytest.mark.asyncio
async def test_config_contains_all_status_commands():
    text = await tn._handle_config_command(chat_id=None)
    for needed in ("/kalender_status", "/archiv_status",
                   "/lexware_status", "/microsoft_status",
                   "/microsoft_check", "/werkstatt_status"):
        assert needed in text, f"/config missing status command {needed}"


@pytest.mark.asyncio
async def test_config_contains_mitarbeiter_block():
    text = await tn._handle_config_command(chat_id=None)
    for needed in ("/mitarbeiter", "/team", "/krank", "/urlaub", "/zurueck"):
        assert needed in text, f"/config missing mitarbeiter command {needed}"


@pytest.mark.asyncio
async def test_config_contains_onboarding_and_status():
    text = await tn._handle_config_command(chat_id=None)
    assert "/onboarding" in text
    assert "/status" in text


@pytest.mark.asyncio
async def test_config_points_to_help():
    """Symmetrie: /config verweist auf /help fuer den Daily-Zugang."""
    text = await tn._handle_config_command(chat_id=None)
    assert "/help" in text


@pytest.mark.asyncio
async def test_config_does_not_contain_daily_workflow_commands():
    """/config soll NICHT mit Daily-Sachen ueberlappen."""
    text = await tn._handle_config_command(chat_id=None)
    for forbidden in ("/aufnahme", "/briefing", "/neue_termine",
                      "/angebot", "/auftraege", "/beleg",
                      "/rechnung", "/material", "/wissen",
                      "/visualisierung",
                      "/storno", "/kunde"):
        assert forbidden not in text, (
            f"/config unexpectedly contains daily command {forbidden}"
        )
