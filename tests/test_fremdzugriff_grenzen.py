"""Drei Grenzen, die an ihrem Rand undicht waren.

Alle drei folgen demselben Muster: ein Rueckfall, der beim Schreiben
sinnvoll ist, wurde beim Lesen mitgenommen — und lieferte dann fremde
Daten unter eigenem Namen.
"""
from __future__ import annotations

import inspect

import pytest


# =====================================================================
# Kalender: kein eigener Token heisst leere Liste, nicht "Kalender des Chefs"
# =====================================================================

@pytest.mark.asyncio
async def test_geplante_termine_ohne_eigenen_kalender_sind_leer(monkeypatch):
    """Der Lesepfad fragt strikt und haelt ein None aus.

    Ohne das faellt der Token-Lookup auf den Inhaber zurueck und cal_id
    auf dessen Primaerkalender — jeder Mitarbeiter ohne verbundenen
    Kalender laese damit dessen Betreffs und Orte. Im Auslieferungs-
    zustand ist das jeder.
    """
    import uuid

    import core.api.app_screens as app_screens
    import plugins.kalender.adapters as adapters

    gesehen = {}

    async def _adapter(tid, emp=None, fallback_calendar_id="primary", strict=False):
        gesehen["strict"] = strict
        return None

    monkeypatch.setattr(adapters, "get_calendar_adapter", _adapter)
    events = await app_screens._geplante_kalendertermine(
        uuid.uuid4(), uuid.uuid4(), tage=7,
    )
    assert events == []
    assert gesehen["strict"] is True, (
        "Der einzige Lesepfad fuer Termin-Betreffs muss strikt fragen."
    )


def test_lesepfad_fragt_strikt():
    """Soll-Bestand an der Quelle — der Aufruf darf das strict nicht verlieren."""
    import core.api.app_screens as app_screens

    quelle = inspect.getsource(app_screens._geplante_kalendertermine)
    assert "strict=True" in quelle


# =====================================================================
# Lexware: kein Rueckfall auf das Plattformkonto
# =====================================================================

def test_voice_lexware_faellt_nicht_auf_global_zurueck():
    """`upsert_customer_contact` sucht per Namen.

    Mit dem _global-Key landeten Endkunden mehrerer Betriebe in EINEM
    Buchhaltungskonto und konnten sich gegenseitig ueberschreiben.
    """
    from plugins.voice_init import handler as voice_handler

    quelle = inspect.getsource(voice_handler.Plugin._get_lexware_provider)
    # Kommentare zaehlen nicht — die erklaeren gerade, warum es weg ist.
    code = "\n".join(
        z for z in quelle.splitlines() if not z.strip().startswith("#")
    )
    assert "_global" not in code, (
        "Der Voice-Pfad darf nicht auf den Lexware-Key der Plattform fallen."
    )
    assert "tc.enabled" in code, (
        "Wie die vier anderen Lexware-Factories: ein abgeschalteter "
        "Schalter muss auch hier wirken."
    )


# =====================================================================
# Push: abmelden nur im eigenen Betrieb
# =====================================================================

def test_push_unsubscribe_ist_tenant_gescoped():
    """`endpoint` ist global eindeutig — ohne Tenant-Filter konnte ein
    Betrieb das Geraet eines anderen stumm schalten. Das Gegenstueck
    (subscribe) zieht die Grenze laengst."""
    import core.api.app_routes as app_routes

    quelle = inspect.getsource(app_routes.app_push_unsubscribe)
    assert "PushSubscription.tenant_id == tenant_id" in quelle
