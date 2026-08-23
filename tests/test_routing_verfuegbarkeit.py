"""Routing + Verfuegbarkeit: Skills, Abwesenheit, Arbeitszeit-Ende.

Reine Logik, keine DB. Die Faelle stammen aus einem Audit gegen die
echten Betriebsdaten — dort war jeder einzelne davon kaputt oder blind.
"""
from __future__ import annotations

import datetime as dt

import pytest

from core.routing.employee_router import (
    _falte,
    extract_skills_from_text,
    skill_formen,
)


# =====================================================================
# Skills: Freitext-Eingabe gegen kanonisches Vokabular
#
# Das Eingabefeld in der App ist freier Text, der Abgleich lief gegen
# eine feste Liste kleingeschriebener Skill-Namen — per exaktem
# Set-Vergleich. Ergebnis: "Heizung" traf nie "heizung", "Sanitär" nie
# "sanitaer", "Tischlerarbeiten" nie "tischler". Selbst wer den
# Platzhalter der App ("z.B. Heizung, Sanitär, Elektro") woertlich
# abtippte, bekam null Treffer und das Routing fiel still auf den
# Inhaber zurueck.
# =====================================================================

def _treffer(skills: list[str], anliegen: str) -> int:
    """Bildet die Zaehlung aus choose_employee nach."""
    formen = skill_formen(skills)
    needed = extract_skills_from_text(anliegen)
    gefaltet = _falte(anliegen)
    return (
        sum(1 for sk in needed if sk in formen)
        + sum(1 for f in formen
              if f not in needed and len(f) >= 4 and f in gefaltet)
    )


@pytest.mark.parametrize("skills,anliegen", [
    # genau der Platzhalter, den die App vorschlaegt
    (["Heizung", "Sanitär", "Elektro"], "Unsere Heizung tropft"),
    (["Heizung", "Sanitär", "Elektro"], "Die Steckdose ist kaputt"),
    # Beugungsformen, wie sie Menschen wirklich eintragen
    (["Tischlerarbeiten"], "Wir brauchen einen Tischler"),
    (["Dachdeckerei"], "Am Dach fehlen Ziegel"),
    # Gewerk, das im festen Vokabular gar nicht vorkommt
    (["Treppenbau"], "Wir wollen Treppenbau machen lassen"),
    (["Trockenbau"], "Wir brauchen Trockenbau im Dachgeschoss"),
])
def test_freitext_skills_treffen(skills, anliegen):
    assert _treffer(skills, anliegen) > 0, (
        f"{skills} sollte auf {anliegen!r} passen"
    )


@pytest.mark.parametrize("skills,anliegen", [
    (["Heizung"], "Wir wollen die Wand streichen lassen"),
    (["Treppenbau"], "Die Heizung ist ausgefallen"),
])
def test_unpassende_skills_treffen_nicht(skills, anliegen):
    """Sonst gewinnt irgendwer, und das Routing wird beliebig."""
    assert _treffer(skills, anliegen) == 0


def test_skill_formen_liefert_roh_und_kanonisch():
    formen = skill_formen(["Tischlerarbeiten", "Sanitär"])
    assert "tischler" in formen        # kanonisch
    assert "tischlerarbeiten" in formen  # Rohform
    assert "sanitaer" in formen        # Umlaut aufgeloest


def test_skill_formen_vertraegt_leere_eingabe():
    assert skill_formen(None) == set()
    assert skill_formen([]) == set()
    # Zu kurze Schnipsel wuerden sonst ueberall matchen
    assert "ab" not in skill_formen(["ab"])


# =====================================================================
# Arbeitszeit: das Termin-ENDE zaehlt mit
# =====================================================================

@pytest.mark.asyncio
async def test_termin_ueber_feierabend_gilt_nicht_als_verfuegbar(monkeypatch):
    """Ein Drei-Stunden-Termin um 16:45 laeuft bis 19:45.

    Geprueft wurde vorher nur die Startzeit — der Mitarbeiter galt als
    verfuegbar, und Q buchte ihm den Feierabend voll.
    """
    import core.models.employee_absence as ea

    emp = type("E", (), {
        "id": "e1", "is_active": True,
        "arbeitstage": [0, 1, 2, 3, 4],
        "arbeitszeiten": {"start": "08:00", "end": "17:00"},
    })()

    class _Sess:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def execute(self, stmt):
            return type("R", (), {"scalar_one_or_none": lambda s=None: emp})()

    monkeypatch.setattr("core.database.AsyncSessionLocal", lambda: _Sess())
    monkeypatch.setattr(ea, "is_employee_absent_on", _false)

    mittwoch = dt.datetime(2026, 8, 26, 16, 45)   # ein Werktag
    assert await ea.is_employee_working_at(emp.id, mittwoch) is True
    assert await ea.is_employee_working_at(
        emp.id, mittwoch, dauer_minuten=180) is False
    # 15:30 + 90 min endet exakt um 17:00 — das ist noch Arbeitszeit
    assert await ea.is_employee_working_at(
        emp.id, mittwoch.replace(hour=15, minute=30), dauer_minuten=90) is True


async def _false(*a, **kw):
    return False
