"""Tests fuer /auftraege Übersicht (refactored 2026-05-17).

Deckt:
- Fertige Auftraege (RECHNUNG_GESENDET) erscheinen NICHT in der Liste
- WORK_DONE (Edge: Rechnungs-Retry) bleibt drin
- State wird prominent als Klartext-Label pro Eintrag angezeigt
- Empty-State erwaehnt /kunde als Wo-finde-ich-fertige-Auftraege-Hint
"""
from __future__ import annotations

import datetime as dt
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.telegram_notify import handler as tn
from core.models.angebot import (
    ANGEBOT_STATUS_RECHNUNG_ERSTELLT,
    ANGEBOT_STATUS_ACCEPTED,
    ANGEBOT_STATUS_WORK_IN_PROGRESS,
    ANGEBOT_STATUS_WORK_DONE,
    ANGEBOT_STATUS_RECHNUNG_GESENDET,
    ANGEBOT_STATUS_ABGEBROCHEN,
)


# =====================================================================
# Doubles
# =====================================================================

def _make_tenant():
    return SimpleNamespace(id=uuid.uuid4(), slug="demo")


def _make_angebot(*, status, kunde_name="Test Kunde",
                  brutto=1000.0, days_ago=2):
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        kunde_name=kunde_name,
        gesamtbetrag_brutto_eur=brutto,
        status=status,
        created_at=dt.datetime(2026, 5, 17) - dt.timedelta(days=days_ago),
        kunde_email=None,
        lexware_quotation_id=None,
        lexware_invoice_id=None,
    )


class _StaticResult:
    """Mimics SQLAlchemy Result für scalars().all() pattern."""

    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return list(self._items)


def _patch_session_with_rows(monkeypatch, rows):
    class _S:
        async def execute(self, _stmt):
            return _StaticResult(rows)

    @asynccontextmanager
    async def cm():
        yield _S()

    monkeypatch.setattr(tn, "AsyncSessionLocal", cm)


def _patch_tenant(monkeypatch, tenant):
    monkeypatch.setattr(tn, "_get_tenant_by_chat",
                        AsyncMock(return_value=tenant))


# =====================================================================
# /auftraege: Listing
# =====================================================================

@pytest.mark.asyncio
async def test_auftraege_active_stati_excludes_rechnung_gesendet():
    """Sanity-Check der Konstante — fertige Auftraege sind explizit raus."""
    assert ANGEBOT_STATUS_RECHNUNG_GESENDET not in tn._AUFTRAG_ACTIVE_STATI
    assert ANGEBOT_STATUS_ABGEBROCHEN not in tn._AUFTRAG_ACTIVE_STATI


@pytest.mark.asyncio
async def test_auftraege_active_stati_includes_work_done_for_retry():
    """WORK_DONE bleibt drin damit Rechnungs-Versand-Retry moeglich ist."""
    assert ANGEBOT_STATUS_WORK_DONE in tn._AUFTRAG_ACTIVE_STATI
    for s in (ANGEBOT_STATUS_RECHNUNG_ERSTELLT, ANGEBOT_STATUS_ACCEPTED,
              ANGEBOT_STATUS_WORK_IN_PROGRESS):
        assert s in tn._AUFTRAG_ACTIVE_STATI


@pytest.mark.asyncio
async def test_auftraege_empty_message_hints_at_kunde(monkeypatch):
    """Bei leerer Liste: Hinweis dass fertige via /kunde gefunden werden."""
    _patch_tenant(monkeypatch, _make_tenant())
    _patch_session_with_rows(monkeypatch, [])
    reply = await tn._handle_auftraege_command(chat_id=1)
    assert "Keine laufenden Auftraege" in reply
    assert "/kunde" in reply  # User soll wissen wo fertige Auftraege sind


@pytest.mark.asyncio
async def test_auftraege_shows_state_label_prominently(monkeypatch):
    """State steht als Klartext-Label pro Eintrag, nicht nur als Progress-Emojis."""
    _patch_tenant(monkeypatch, _make_tenant())
    rows = [
        _make_angebot(status=ANGEBOT_STATUS_WORK_IN_PROGRESS,
                      kunde_name="Sven Jantos", brutto=2450.0),
        _make_angebot(status=ANGEBOT_STATUS_ACCEPTED,
                      kunde_name="Frau Mueller", brutto=850.0, days_ago=3),
    ]
    _patch_session_with_rows(monkeypatch, rows)
    reply = await tn._handle_auftraege_command(chat_id=1)

    # State-Labels als Klartext (mit Emoji) sichtbar
    assert "🔨 Arbeit laeuft" in reply
    assert "✅ Angenommen" in reply
    # Kunden + Betraege
    assert "Sven Jantos" in reply
    assert "2450.00€" in reply or "2,450.00€" in reply or "2450€" in reply
    assert "Frau Mueller" in reply
    # Anzahl im Header
    assert "(2)" in reply


@pytest.mark.asyncio
async def test_auftraege_each_entry_has_clickable_detail_command(monkeypatch):
    """Pro Eintrag ein /auftrag_<8hex>-Befehl zum Reinklicken."""
    _patch_tenant(monkeypatch, _make_tenant())
    ang = _make_angebot(status=ANGEBOT_STATUS_RECHNUNG_ERSTELLT)
    _patch_session_with_rows(monkeypatch, [ang])
    reply = await tn._handle_auftraege_command(chat_id=1)
    expected = f"/auftrag_{str(ang.id)[:8]}"
    assert expected in reply


@pytest.mark.asyncio
async def test_auftraege_no_tenant_returns_hint(monkeypatch):
    _patch_tenant(monkeypatch, None)
    reply = await tn._handle_auftraege_command(chat_id=1)
    assert "keinem betrieb" in reply.lower() or "/start" in reply


# =====================================================================
# Progress-Line-Helper
# =====================================================================

def test_progress_line_includes_all_lifecycle_symbols():
    """Progress-Bar zeigt alle 5 Schritte mit Strikethrough/Bold/Italic."""
    line = tn._auftrag_progress_line(ANGEBOT_STATUS_WORK_IN_PROGRESS)
    # ARBEIT_LAEUFT ist Schritt 3/5 — sollte Bold auf 🔨 sein,
    # Strikethrough auf 📋 und ✅, Italic auf 🏁 und 📨
    assert "<s>📋</s>" in line
    assert "<s>✅</s>" in line
    assert "<b>🔨</b>" in line
    assert "<i>🏁</i>" in line
    assert "<i>📨</i>" in line


def test_progress_line_first_step_no_strikethrough():
    line = tn._auftrag_progress_line(ANGEBOT_STATUS_RECHNUNG_ERSTELLT)
    # Schritt 1/5 — kein <s>
    assert "<s>" not in line
    assert "<b>📋</b>" in line
