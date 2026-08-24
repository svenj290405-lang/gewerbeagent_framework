"""Tests fuer die Loeschfristen der Mail-Konversationen.

Vorgeschichte (Audit 2026-08-24): ohne ``termin_datum`` loeschte der Job nur
Zeilen im Zustand ``closed`` — den setzt genau EINE Stelle im ganzen Code.
Alles per Mail Stornierte und jeder eingeschlafene Dialog lag deshalb
unbefristet in der Prod-DB, mit Klarname, Mailadresse und Kundentext. Live
gemessen: 91 bis 95 Tage bei einer Zusage von 90 — waehrend der Cron
taeglich "0 Konversationen geloescht" meldete.
"""
from __future__ import annotations

import pytest

from core.models import (
    STATE_AWAITING_CONFIRMATION, STATE_BOOKED, STATE_CLOSED,
    STATE_DELIVERY_FAILED, STATE_DIALOG, STATE_PROPOSING_SLOTS, STATE_STORNIERT,
)
from scripts.cleanup_email_conversations import LOESCHBARE_ZUSTAENDE


def test_stornierte_laufen_in_die_frist():
    """Der Fall, der live 95 Tage liegen blieb."""
    assert STATE_STORNIERT in LOESCHBARE_ZUSTAENDE


def test_eingeschlafene_dialoge_laufen_in_die_frist():
    assert STATE_DIALOG in LOESCHBARE_ZUSTAENDE
    assert STATE_PROPOSING_SLOTS in LOESCHBARE_ZUSTAENDE
    assert STATE_AWAITING_CONFIRMATION in LOESCHBARE_ZUSTAENDE


def test_abgeschlossene_und_unzustellbare_ebenfalls():
    assert STATE_CLOSED in LOESCHBARE_ZUSTAENDE
    assert STATE_DELIVERY_FAILED in LOESCHBARE_ZUSTAENDE


def test_gebuchte_termine_werden_nicht_ueber_den_zustand_geloescht():
    """Ein Termin im Kalender ist ein laufender Vorgang — der wird ueber
    ``termin_datum`` faellig, nicht ueber den Zustand."""
    assert STATE_BOOKED not in LOESCHBARE_ZUSTAENDE


def test_jeder_zustand_hat_eine_entscheidung():
    """Waechter: kommt ein neuer Zustand dazu, muss hier jemand hinsehen —
    sonst entsteht lautlos die naechste Tabelle ohne Frist."""
    from core.models import email_conversation as ec

    alle = {
        wert for name, wert in vars(ec).items()
        if name.startswith("STATE_") and isinstance(wert, str)
    }
    entschieden = set(LOESCHBARE_ZUSTAENDE) | {STATE_BOOKED}
    assert alle == entschieden, (
        f"Ohne Entscheidung: {alle - entschieden}. Entweder in "
        f"LOESCHBARE_ZUSTAENDE aufnehmen oder hier begruenden."
    )
