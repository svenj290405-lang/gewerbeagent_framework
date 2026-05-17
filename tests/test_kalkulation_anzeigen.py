"""Tests fuer /kalkulation_anzeigen (Fix 2026-05-17): alle Eintraege
muessen sichtbar sein, auch solche mit unbekannten Kategorien. Plus
semantische Beschreibung pro Eintrag.

Vorher: der Render-Loop iterierte nur ueber ALLE_KALK_KATEGORIEN.
Eintraege mit einer kategorie die nicht in der Liste war (z.B. durch
manuelle DB-Edit oder Legacy-Migration) wurden unsichtbar.
"""
from __future__ import annotations

import datetime as dt
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.telegram_notify import handler as tn


# =====================================================================
# Doubles
# =====================================================================

class _StaticResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return list(self._items)


def _patch_session_with_entries(monkeypatch, entries):
    class _S:
        async def execute(self, _stmt):
            return _StaticResult(entries)

    @asynccontextmanager
    async def cm():
        yield _S()

    monkeypatch.setattr(tn, "AsyncSessionLocal", cm)


def _make_tenant():
    return SimpleNamespace(
        id=uuid.uuid4(),
        slug="demo",
        company_name="Schreinerei Test",
    )


def _make_entry(
    *,
    name="X",
    formel="a+b",
    kategorie="sonstiges",
    beschreibung=None,
    einheit=None,
    source="manual",
):
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        formel=formel,
        kategorie=kategorie,
        beschreibung=beschreibung,
        einheit=einheit,
        source=source,
        sortierung=0,
        created_at=dt.datetime(2026, 5, 17),
        aktiv=True,
    )


# =====================================================================
# Tests
# =====================================================================

@pytest.mark.asyncio
async def test_anzeigen_shows_entry_with_known_kategorie(monkeypatch):
    """Smoke-Test: 1 Eintrag mit bekannter Kategorie -> taucht auf."""
    monkeypatch.setattr(
        tn, "_get_tenant_by_chat",
        AsyncMock(return_value=_make_tenant()),
    )
    entry = _make_entry(name="Anfahrt", formel="km*2", kategorie="anfahrt")
    _patch_session_with_entries(monkeypatch, [entry])
    reply = await tn._handle_kalkulation_anzeigen(chat_id=1)
    assert "Anfahrt" in reply
    assert "km*2" in reply


@pytest.mark.asyncio
async def test_anzeigen_shows_entry_with_unknown_kategorie(monkeypatch):
    """Eintrag mit kategorie='foobar' (nicht in ALLE_KALK_KATEGORIEN)
    muss trotzdem angezeigt werden — vorher wurde der unsichtbar."""
    monkeypatch.setattr(
        tn, "_get_tenant_by_chat",
        AsyncMock(return_value=_make_tenant()),
    )
    entry = _make_entry(name="Legacy-Eintrag", kategorie="foobar")
    _patch_session_with_entries(monkeypatch, [entry])
    reply = await tn._handle_kalkulation_anzeigen(chat_id=1)
    assert "Legacy-Eintrag" in reply
    # Counter zaehlt ihn auch mit
    assert "1 Regel" in reply


@pytest.mark.asyncio
async def test_anzeigen_mixed_known_and_unknown_kategorien(monkeypatch):
    """Bekannte Kategorien zuerst (in ALLE_KALK_KATEGORIEN-Reihenfolge),
    dann unbekannte sortiert."""
    monkeypatch.setattr(
        tn, "_get_tenant_by_chat",
        AsyncMock(return_value=_make_tenant()),
    )
    entries = [
        _make_entry(name="A-Manuell", kategorie="anfahrt"),
        _make_entry(name="B-Legacy", kategorie="zzz_unknown"),
        _make_entry(name="C-Material", kategorie="material"),
    ]
    _patch_session_with_entries(monkeypatch, entries)
    reply = await tn._handle_kalkulation_anzeigen(chat_id=1)
    # Alle drei sichtbar
    assert "A-Manuell" in reply
    assert "B-Legacy" in reply
    assert "C-Material" in reply
    # 3 Regeln zaehlt
    assert "3 Regel" in reply
    # Anfahrt vor Material (ALLE_KALK_KATEGORIEN-Reihenfolge), beide vor Legacy
    pos_anfahrt = reply.index("A-Manuell")
    pos_material = reply.index("C-Material")
    pos_legacy = reply.index("B-Legacy")
    assert pos_anfahrt < pos_material < pos_legacy


@pytest.mark.asyncio
async def test_anzeigen_renders_beschreibung_when_present(monkeypatch):
    """Eintrag mit beschreibung (vom Classifier oder Wizard) -> Text sichtbar."""
    monkeypatch.setattr(
        tn, "_get_tenant_by_chat",
        AsyncMock(return_value=_make_tenant()),
    )
    entry = _make_entry(
        name="Standard-Treppe",
        formel="a+b+c",
        kategorie="pauschale",
        beschreibung="Gesamtpreis einer Standard-Treppe inkl. Material",
    )
    _patch_session_with_entries(monkeypatch, [entry])
    reply = await tn._handle_kalkulation_anzeigen(chat_id=1)
    assert "Standard-Treppe" in reply
    assert "Gesamtpreis einer Standard-Treppe" in reply


@pytest.mark.asyncio
async def test_anzeigen_no_crash_on_null_kategorie(monkeypatch):
    """Defensiv: kategorie=None (sollte nicht vorkommen, aber DB-Edit
    koennte's tun) -> kein Crash, Eintrag landet unter 'Ohne Kategorie'."""
    monkeypatch.setattr(
        tn, "_get_tenant_by_chat",
        AsyncMock(return_value=_make_tenant()),
    )
    entry = _make_entry(name="X", kategorie=None)
    _patch_session_with_entries(monkeypatch, [entry])
    reply = await tn._handle_kalkulation_anzeigen(chat_id=1)
    assert "X" in reply


@pytest.mark.asyncio
async def test_anzeigen_excel_source_tag(monkeypatch):
    """Excel-Eintraege bekommen "(Excel)"-Suffix damit Daniel sieht
    woher die Regel kommt."""
    monkeypatch.setattr(
        tn, "_get_tenant_by_chat",
        AsyncMock(return_value=_make_tenant()),
    )
    entries = [
        _make_entry(name="Manuell-A", source="manual"),
        _make_entry(name="Excel-B", source="excel"),
    ]
    _patch_session_with_entries(monkeypatch, entries)
    reply = await tn._handle_kalkulation_anzeigen(chat_id=1)
    # Excel-Eintrag hat "(Excel)"-Marker, manueller nicht
    assert "Excel-B" in reply
    # Suche das (Excel)-Tag nahe Excel-B (innerhalb 50 chars):
    pos = reply.index("Excel-B")
    assert "(Excel)" in reply[pos:pos + 60]
    pos_man = reply.index("Manuell-A")
    assert "(Excel)" not in reply[pos_man:pos_man + 60]
