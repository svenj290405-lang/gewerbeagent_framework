"""Tests fuer die Zeilen-Sichtbarkeit bei Auftraegen
(core/security/app_scope.py).

Das Routen-Gate beantwortet "darf er den Endpunkt aufrufen", dieser
Filter "welche Zeilen sieht er darin". Vor der Umstellung gab es diese
Ebene im ganzen HTTP-Layer nur an einer Stelle (fremde Stunden loeschen).

Die Tests pruefen die Filter-Bedingungen selbst, nicht die SQL-
Ausfuehrung — entscheidend ist, DASS gefiltert wird und dass ein Auftrag
ohne Besitzer nicht durchrutscht.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from core.models.angebot import Angebot
from core.security.app_scope import AuftragScope, auftrag_filter, auftrag_scope


def _req(perms, emp_id=None):
    return SimpleNamespace(state=SimpleNamespace(
        app_employee=SimpleNamespace(id=emp_id or uuid.uuid4()),
        app_permissions=frozenset(perms),
    ))


# =====================================================================
# Scope aus der Session ableiten
# =====================================================================

def test_mit_recht_sieht_alle():
    scope = auftrag_scope(_req({"auftraege.alle_sehen"}))
    assert scope.alle is True


def test_ohne_recht_nur_eigene():
    emp_id = uuid.uuid4()
    scope = auftrag_scope(_req(set(), emp_id))
    assert scope.alle is False
    assert scope.employee_id == emp_id


def test_fehlende_permissions_sind_restriktiv():
    """Kein app_permissions im State (z.B. falsch verdrahteter Handler)
    darf NICHT bedeuten 'sieht alles'."""
    req = SimpleNamespace(state=SimpleNamespace(
        app_employee=SimpleNamespace(id=uuid.uuid4()),
    ))
    assert auftrag_scope(req).alle is False


# =====================================================================
# Filter-Bedingungen
# =====================================================================

def test_alle_sehen_erzeugt_keinen_filter():
    assert auftrag_filter(AuftragScope(alle=True, employee_id=uuid.uuid4())) == []


def test_eingeschraenkt_filtert_auf_zuweisung():
    emp_id = uuid.uuid4()
    bedingungen = auftrag_filter(AuftragScope(alle=False, employee_id=emp_id))
    assert len(bedingungen) == 1
    # Die Bedingung muss die Zuweisungsspalte betreffen.
    assert "assigned_employee_id" in str(bedingungen[0])


def test_nicht_zugewiesene_auftraege_rutschen_nicht_durch():
    """assigned_employee_id IS NULL gehoert bewusst NICHT in den Filter:
    ein Auftrag ohne Besitzer ist fuer eingeschraenkte Nutzer unsichtbar
    (fail-closed). Die Migration hat Bestandsauftraege deshalb dem
    Inhaber zugeordnet."""
    bedingungen = auftrag_filter(AuftragScope(alle=False, employee_id=uuid.uuid4()))
    kompiliert = str(bedingungen[0]).lower()
    assert "is null" not in kompiliert
    assert " or " not in kompiliert


def test_filter_ist_gegen_angebot_gebaut():
    bedingungen = auftrag_filter(AuftragScope(alle=False, employee_id=uuid.uuid4()))
    links = bedingungen[0].left
    assert links.name == "assigned_employee_id"
    assert links.table.name == "angebote"


# =====================================================================
# Wer neu anlegt, bekommt den Auftrag auch zugewiesen
#
# Audit 2026-08-24: ``POST /app/api/angebote/anlegen`` war der einzige der
# drei Anlege-Wege ohne ``assigned_employee_id``. Der Filter oben ist
# fail-closed — ein so angelegter Auftrag war fuer jeden ohne
# ``auftraege.alle_sehen`` unsichtbar, dauerhaft.
# =====================================================================

@pytest.mark.asyncio
async def test_neues_angebot_bekommt_einen_besitzer(monkeypatch):
    import json

    from core.api import app_screens
    from core.services import document_flow

    anleger = uuid.uuid4()
    aufruf: dict = {}

    async def _create(tid, **kw):
        aufruf.update(kw)
        return {"ok": True, "id": "x"}
    monkeypatch.setattr(document_flow, "create_angebot", _create)

    async def _feature(tid, key):
        return True
    import core.features.check as check
    monkeypatch.setattr(check, "is_feature_enabled", _feature)

    tid = uuid.uuid4()
    body = {"kunde_name": "Meier", "positionen": [{"name": "X", "menge": 1,
                                                   "preis_brutto_eur": 10}]}

    async def _json():
        return body
    req = SimpleNamespace(
        json=_json,
        state=SimpleNamespace(app_employee=SimpleNamespace(id=anleger),
                              app_tenant=SimpleNamespace(id=tid)),
    )
    monkeypatch.setattr(app_screens, "current_tenant_id", lambda r: tid)

    resp = await app_screens.api_angebot_anlegen(request=req, _e=None, _c=None)

    assert resp.status_code == 200
    assert json.loads(resp.body)["ok"] is True
    assert aufruf["assigned_employee_id"] == anleger
