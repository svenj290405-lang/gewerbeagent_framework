"""Tests fuer den Umsatzsteuersatz in Angeboten und Rechnungen.

Vorgeschichte (Audit 2026-08-24): der Satz war im gesamten PWA-Weg hart auf
19 % verdrahtet — Backend wie Formular. Ein Photovoltaik-Betrieb
(Nullsteuersatz nach § 12 Abs. 3 UStG) haette auf jeder App-Rechnung
Umsatzsteuer ausgewiesen, die er nicht ausweisen darf.

Die zweite, subtilere Haelfte des Fundes: ueberall stand ``int(x or 19)`` —
und ``0 or 19`` ist 19. Selbst ein ausdruecklich gesetzter Nullsatz waere
still zu 19 % geworden. Genau das pruefen die Tests hier.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import core.services.document_flow as df
from core.services import buchhaltung as buch


def _patch_toolconfig(monkeypatch, cfg):
    """Faked die ToolConfig(lexware).config-Abfrage."""
    class _S:
        async def execute(self, stmt):
            return SimpleNamespace(scalar_one_or_none=lambda: cfg)

    @asynccontextmanager
    async def _gs():
        yield _S()

    import core.database.connection as conn
    monkeypatch.setattr(conn, "get_session", _gs)


# --------------------------------------------------------------------------
# _mwst_oder: 0 ist ein Wert, kein "leer"
# --------------------------------------------------------------------------

def test_null_prozent_bleibt_null():
    assert df._mwst_oder(0, 19) == 0


def test_fehlender_satz_nimmt_den_standard():
    assert df._mwst_oder(None, 7) == 7


def test_kaputter_satz_nimmt_den_standard():
    assert df._mwst_oder("neunzehn", 19) == 19


# --------------------------------------------------------------------------
# Positionen -> Lexware-LineItems
# --------------------------------------------------------------------------

def test_position_ohne_satz_erbt_den_betriebssatz():
    items, _g, err = df._positionen_to_line_items(
        [{"name": "PV-Anlage", "menge": 1, "preis_brutto_eur": 10000}],
        standard_mwst=0)
    assert err is None
    assert items[0].tax_rate_percent == 0


def test_position_mit_eigenem_satz_gewinnt():
    items, _g, err = df._positionen_to_line_items(
        [{"name": "Buch", "menge": 1, "preis_brutto_eur": 20, "mwst_prozent": 7}],
        standard_mwst=19)
    assert err is None
    assert items[0].tax_rate_percent == 7


def test_ausdrueckliche_null_wird_nicht_zu_neunzehn():
    items, _g, _e = df._positionen_to_line_items(
        [{"name": "PV", "menge": 1, "preis_brutto_eur": 100, "mwst_prozent": 0}],
        standard_mwst=19)
    assert items[0].tax_rate_percent == 0


# --------------------------------------------------------------------------
# Betriebs-Standard aus der ToolConfig
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_betriebssatz_kommt_aus_der_toolconfig(monkeypatch):
    _patch_toolconfig(monkeypatch, {"mwst_standard": 0})
    assert await buch.mwst_standard(uuid.uuid4()) == 0


@pytest.mark.asyncio
async def test_ohne_eintrag_gilt_neunzehn(monkeypatch):
    _patch_toolconfig(monkeypatch, {})
    assert await buch.mwst_standard(uuid.uuid4()) == 19


@pytest.mark.asyncio
async def test_unsinniger_satz_kippt_nichts(monkeypatch):
    """Ein Tippfehler in der Konfiguration darf keine krumme Steuer auf eine
    echte Rechnung schreiben."""
    _patch_toolconfig(monkeypatch, {"mwst_standard": 13})
    assert await buch.mwst_standard(uuid.uuid4()) == 19


@pytest.mark.asyncio
async def test_kaputte_toolconfig_kippt_nichts(monkeypatch):
    _patch_toolconfig(monkeypatch, {"mwst_standard": "null"})
    assert await buch.mwst_standard(uuid.uuid4()) == 19
