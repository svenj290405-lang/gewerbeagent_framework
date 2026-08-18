"""Tests fuer die OAuth-Scope-Profile (Inhaber vs. Mitarbeiter)
und den strikten Token-Lookup.

Hintergrund: ein Mitarbeiter verbindet sein PRIVATES Konto. Voller
Kalenderzugriff waere dort eine Zusage, die wir technisch nicht halten
koennen. Google laesst sich eng schnueren (nur Belegung + ein von uns
angelegter Zweitkalender), Outlook nicht — dort entfaellt aber immerhin
jeder Mail-Scope.
"""
from __future__ import annotations

import uuid

import pytest

from core.security import oauth_flow as of


# =====================================================================
# Google-Profile
# =====================================================================

def test_inhaber_profil_ist_der_bisherige_scope():
    assert of.google_scopes(of.SCOPE_PROFIL_VOLL) == of.GOOGLE_SCOPES
    assert of.google_scopes(None) == of.GOOGLE_SCOPES


def test_mitarbeiter_bekommt_keinen_vollen_kalender():
    scopes = of.google_scopes(of.SCOPE_PROFIL_MITARBEITER)
    assert "https://www.googleapis.com/auth/calendar" not in scopes
    assert "https://www.googleapis.com/auth/calendar.freebusy" in scopes
    assert "https://www.googleapis.com/auth/calendar.app.created" in scopes


def test_mitarbeiter_bekommt_kein_drive():
    """Das Kundenarchiv haengt am Betriebskonto, nicht am privaten."""
    scopes = of.google_scopes(of.SCOPE_PROFIL_MITARBEITER)
    assert not any("drive" in s for s in scopes)


def test_unbekanntes_profil_faellt_auf_voll():
    """Bestandsdaten und alte States haben kein Profil — die duerfen
    nicht plotzlich mit halben Rechten scheitern."""
    assert of.google_scopes("quatsch") == of.GOOGLE_SCOPES


# =====================================================================
# Microsoft-Profile
# =====================================================================

def test_mitarbeiter_gibt_kein_postfach_frei():
    """Der wichtigste Punkt bei Outlook: bisher haette ein Monteur, der
    seinen Kalender verbindet, sein komplettes Postfach mitgegeben."""
    scopes = of.microsoft_scopes(of.SCOPE_PROFIL_MITARBEITER)
    assert not any(s.startswith("Mail.") for s in scopes)
    assert "Mail.ReadWrite" in of.MICROSOFT_SCOPES  # Inhaber schon


def test_mitarbeiter_behaelt_kalender_schreibrecht():
    """Graph kennt keine Entsprechung zu calendar.app.created — zum
    Anlegen von Terminen braucht es Calendars.ReadWrite. Das ist die
    dokumentierte Einschraenkung, kein Versehen."""
    scopes = of.microsoft_scopes(of.SCOPE_PROFIL_MITARBEITER)
    assert "Calendars.ReadWrite" in scopes


def test_microsoft_default_bleibt_voll():
    assert of.microsoft_scopes(None) == of.MICROSOFT_SCOPES


# =====================================================================
# Strikter Token-Lookup
# =====================================================================

@pytest.mark.asyncio
async def test_strict_faellt_nicht_auf_den_inhaber_zurueck(monkeypatch):
    """Ohne strict landet ein Termin "fuer Marco" still im Kalender des
    Chefs, wenn Marco nie verbunden hat — und niemand merkt es."""
    from core.security import oauth_token_lookup as otl

    class _Result:
        def scalar_one_or_none(self):
            return None

    class _Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def execute(self, *a, **kw): return _Result()

    monkeypatch.setattr(otl, "AsyncSessionLocal", lambda: _Session())

    gerufen = {"default": False}

    async def _default_emp(tenant_id):
        gerufen["default"] = True
        return None

    monkeypatch.setattr(
        "core.models.employee.get_default_employee", _default_emp,
    )

    tok = await otl.find_oauth_token(
        uuid.uuid4(), "google", employee_id=uuid.uuid4(), strict=True,
    )
    assert tok is None
    assert gerufen["default"] is False, (
        "strict darf den Default-Employee gar nicht erst anfragen"
    )


@pytest.mark.asyncio
async def test_ohne_strict_bleibt_der_fallback(monkeypatch):
    """Der Legacy-Pfad (employee_id=None) soll sich nicht aendern."""
    from core.security import oauth_token_lookup as otl

    class _Result:
        def scalar_one_or_none(self):
            return None

    class _Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def execute(self, *a, **kw): return _Result()

    monkeypatch.setattr(otl, "AsyncSessionLocal", lambda: _Session())

    gerufen = {"default": False}

    async def _default_emp(tenant_id):
        gerufen["default"] = True
        return None

    monkeypatch.setattr(
        "core.models.employee.get_default_employee", _default_emp,
    )

    await otl.find_oauth_token(uuid.uuid4(), "google", employee_id=uuid.uuid4())
    assert gerufen["default"] is True
