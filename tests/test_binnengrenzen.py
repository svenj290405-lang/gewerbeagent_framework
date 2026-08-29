"""Grenzen INNERHALB eines Betriebs — und die eine zwischen Betrieben,
die der Login zog.

Gemeinsames Muster: ein zweiter Weg zu denselben Daten (das Briefing
statt /team, Q statt der HTTP-Route) kannte die Grenze nicht, die der
erste Weg zog.
"""
from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


# =====================================================================
# Tagesbriefing: Krankmeldungen sind Gesundheitsdaten
# =====================================================================

def test_briefing_fragt_nach_team_sehen():
    """Das Recht haengt an der ToolSpec, das Briefing rief die Funktion
    direkt auf — und lieferte damit einem Monteur ohne jedes Recht die
    Namen aller heute Kranken (DSGVO Art. 9)."""
    import core.api.app_screens as app_screens

    quelle = inspect.getsource(app_screens._build_briefing_text)
    assert '"team.sehen" in' in quelle, (
        "_run_team_status darf im Briefing nur mit team.sehen laufen."
    )


def test_team_status_traegt_das_recht():
    """Gegenprobe: waere das Recht von der ToolSpec verschwunden, waere
    der Test oben wertlos."""
    from core.ai.command_center import _spec_by_name

    spec = _spec_by_name("team_status")
    assert spec is not None
    assert spec.permission == "team.sehen"


# =====================================================================
# Q kennt die Zeilen-Sichtbarkeit bei Auftraegen
# =====================================================================

def test_q_auftrag_status_nutzt_den_zentralen_filter():
    """`auftrag_filter` wird im HTTP-Layer elfmal angewandt und in Q
    keinmal — ein Nutzer ohne `auftraege.alle_sehen` konnte per Chat
    Auftraege lesen und aendern, die er in der App nicht sieht."""
    import core.ai.command_center as cc

    quelle = inspect.getsource(cc._run_auftrag_status)
    assert "auftrag_filter" in quelle
    assert "auftrag_scope_fuer" in quelle


def test_scope_ohne_recht_schraenkt_ein():
    from core.security.app_scope import auftrag_filter, auftrag_scope_fuer

    emp_id = uuid.uuid4()
    eng = auftrag_scope_fuer(emp_id, frozenset())
    weit = auftrag_scope_fuer(emp_id, frozenset({"auftraege.alle_sehen"}))
    assert len(auftrag_filter(eng)) == 1
    assert auftrag_filter(weit) == []


# =====================================================================
# Aktivierungs-Link nicht mehr im Zugriffslog
# =====================================================================

def test_erfolgsmeldung_reist_als_quittung_nicht_als_klartext():
    """Der Link enthaelt einen 14 Tage gueltigen Einmal-Token. Als `?msg=`
    stand er in Caddys Zugriffslog (336 h) und in der Browser-History."""
    import core.admin.onboarding_routes as onb

    quelle = inspect.getsource(onb.tenant_new_submit)
    assert "?flash=" in quelle
    assert "?msg=" not in quelle


def test_quittung_gilt_nur_einmal():
    from core.admin.onboarding_routes import _flash_holen, _flash_setzen

    q = _flash_setzen("Aktivierungs-Link: https://example/app/activate?token=geheim")
    assert "geheim" in (_flash_holen(q) or "")
    assert _flash_holen(q) is None
    assert _flash_holen("erfunden") is None


# =====================================================================
# Eine Mailadresse gehoert zu genau einem Betrieb
# =====================================================================

class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, _stmt):
        rows = self._rows

        class _R:
            def scalars(self_inner):
                class _S:
                    def all(self_s):
                        return rows

                    def first(self_s):
                        return rows[0] if rows else None
                return _S()
        return _R()


def _emp(tenant_id=None):
    return SimpleNamespace(
        id=uuid.uuid4(), tenant_id=tenant_id or uuid.uuid4(),
        contact_email="chef@betrieb.de", is_active=True,
    )


@pytest.mark.asyncio
async def test_mehrdeutige_adresse_laesst_niemanden_rein():
    """Ohne Unique-Index entschied frueher die Speicherreihenfolge von
    Postgres, in welchem Betrieb jemand landet. Raten waere ein Login im
    falschen Betrieb — die App hat keinen Umschalter."""
    from core.security.app_auth import find_employee_by_email

    s = _FakeSession([_emp(), _emp()])
    assert await find_employee_by_email("chef@betrieb.de", session=s) is None


@pytest.mark.asyncio
async def test_eindeutige_adresse_funktioniert_weiter():
    from core.security.app_auth import find_employee_by_email

    einer = _emp()
    s = _FakeSession([einer])
    assert await find_employee_by_email("chef@betrieb.de", session=s) is einer


@pytest.mark.asyncio
async def test_fremde_adresse_ist_nicht_frei():
    """Der eigentliche Riegel: der mehrdeutige Zustand kann gar nicht
    mehr entstehen. Ein Eingeladener konnte sich die Adresse des Inhabers
    eines FREMDEN Betriebs geben — Besitznachweis gab es keinen."""
    from core.security.app_auth import kontakt_mail_frei

    fremder = _emp()
    s = _FakeSession([fremder])
    assert not await kontakt_mail_frei(
        "chef@betrieb.de", employee_id=uuid.uuid4(), session=s)
    # Die eigene Adresse behalten bleibt erlaubt.
    assert await kontakt_mail_frei(
        "chef@betrieb.de", employee_id=fremder.id, session=s)
