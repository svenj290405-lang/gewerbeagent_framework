"""Zeilen-Sichtbarkeit innerhalb eines Betriebs.

Das Routen-Gate (``enforce_app_permission``) beantwortet "darf dieser
Mitarbeiter diesen Endpunkt aufrufen". Hier geht es um die Frage danach:
"welche ZEILEN darf er darin sehen".

Bisher gab es diese Ebene nur an einer einzigen Stelle im ganzen
HTTP-Layer (``core/services/auftrag_stunden.py``: fremde Stunden
loeschen). Mit dem Recht ``auftraege.alle_sehen`` kommt sie fuer
Auftraege dazu.

Absichtlich EIN Filter statt zwoelf: die Auftrags-Endpunkte fragen
diesen Scope, statt jeweils ihr eigenes ``.where()`` mitzubringen. Ein
duplizierter Filter ist genau die Sorte Code, bei der die zwoelfte
Kopie vergessen wird.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import Request
from sqlalchemy import ColumnElement

from core.models.angebot import Angebot


@dataclass(frozen=True)
class AuftragScope:
    """Wie weit der aktuelle Nutzer bei Auftraegen sehen darf."""

    alle: bool
    employee_id: uuid.UUID


def auftrag_scope(request: Request) -> AuftragScope:
    """Scope aus der laufenden Session ableiten."""
    emp = request.state.app_employee
    perms = getattr(request.state, "app_permissions", frozenset())
    return AuftragScope(alle="auftraege.alle_sehen" in perms, employee_id=emp.id)


def auftrag_filter(scope: AuftragScope) -> list[ColumnElement[bool]]:
    """Zusaetzliche WHERE-Bedingungen fuer Angebot-Queries.

    Leer, wenn der Nutzer alles sehen darf. Sonst: nur die auf ihn
    zugewiesenen Auftraege.

    ``assigned_employee_id IS NULL`` gehoert bewusst NICHT dazu. Ein
    Auftrag ohne Besitzer ist fuer einen eingeschraenkten Nutzer
    unsichtbar (fail-closed) — die Migration hat Bestandsauftraege
    deshalb dem Inhaber zugeordnet, damit nichts still verschwindet.
    """
    if scope.alle:
        return []
    return [Angebot.assigned_employee_id == scope.employee_id]
