"""Jede PWA-Route muss ein Recht deklarieren.

Das ist der wichtigste Test des Rechtesystems. Bei ~125 Routen ist "eine
vergessen" der wahrscheinlichste Fehler, und eine vergessene Route waere
ein stilles Loch: sie liefert weiter tenant-weite Daten an jeden, und
niemandem faellt es auf.

Hier faellt es sofort auf. Wer eine neue Route baut, ohne sie in
core/security/app_permission_routes.py einzutragen, bekommt einen roten
Test mit dem Funktionsnamen.

Die Routen werden aus der echten FastAPI-App gelesen, nicht aus einer
gepflegten Liste — der Test kann also nicht veralten.
"""
from __future__ import annotations

import pytest

from core.api.app import app
from core.features.permissions import RECHTE
from core.security.app_permission_routes import (
    OEFFENTLICHE_ENDPUNKTE,
    OFFEN,
    ROUTE_RECHTE,
)


def _app_endpunkte() -> list[tuple[str, str]]:
    """(Endpunkt-Funktionsname, Pfad) aller /app-Routen."""
    out = []
    for route in app.routes:
        pfad = getattr(route, "path", "")
        if not pfad.startswith("/app"):
            continue
        endpoint = getattr(route, "endpoint", None)
        name = getattr(endpoint, "__name__", None)
        if name is None:
            # StaticFiles-Mount o.ae. — hat keine Endpunkt-Funktion und
            # wird vom Gate ohnehin durchgelassen.
            continue
        out.append((name, pfad))
    return out


def test_jede_route_hat_ein_recht():
    fehlend = sorted({
        f"{name}  ({pfad})"
        for name, pfad in _app_endpunkte()
        if name not in ROUTE_RECHTE and name not in OEFFENTLICHE_ENDPUNKTE
    })
    assert not fehlend, (
        "Diese Endpunkte deklarieren kein Recht. Trage sie in "
        "core/security/app_permission_routes.py ein — entweder mit einem "
        "Recht, mit OFFEN (jeder Eingeloggte) oder, falls sie ohne Login "
        "erreichbar sein sollen, in OEFFENTLICHE_ENDPUNKTE:\n  "
        + "\n  ".join(fehlend)
    )


def test_keine_unbekannten_rechte_in_der_tabelle():
    unbekannt = sorted({
        f"{name} -> {recht}"
        for name, recht in ROUTE_RECHTE.items()
        if recht != OFFEN and recht not in RECHTE
    })
    assert not unbekannt, (
        "Diese Eintraege verweisen auf ein Recht, das es in "
        "core/features/permissions.py nicht gibt:\n  " + "\n  ".join(unbekannt)
    )


def test_keine_karteileichen_in_der_tabelle():
    """Eintraege fuer Endpunkte, die es nicht mehr gibt, sind harmlos —
    aber sie taeuschen Abdeckung vor und sollen aufgeraeumt werden."""
    echte = {name for name, _ in _app_endpunkte()}
    tot = sorted(set(ROUTE_RECHTE) - echte)
    assert not tot, (
        "Diese Eintraege in ROUTE_RECHTE zeigen auf Endpunkte, die es "
        "nicht mehr gibt:\n  " + "\n  ".join(tot)
    )


def test_oeffentliche_endpunkte_existieren():
    echte = {name for name, _ in _app_endpunkte()}
    tot = sorted(OEFFENTLICHE_ENDPUNKTE - echte)
    assert not tot, (
        "Diese Eintraege in OEFFENTLICHE_ENDPUNKTE gibt es nicht mehr:\n  "
        + "\n  ".join(tot)
    )


def test_oeffentlich_und_gerechtet_schliessen_sich_aus():
    doppelt = sorted(set(ROUTE_RECHTE) & OEFFENTLICHE_ENDPUNKTE)
    assert not doppelt, (
        "Diese Endpunkte stehen sowohl in ROUTE_RECHTE als auch in "
        "OEFFENTLICHE_ENDPUNKTE — OEFFENTLICHE_ENDPUNKTE gewinnt, der "
        "Rechte-Eintrag ist also wirkungslos:\n  " + "\n  ".join(doppelt)
    )


def test_geldpfade_sind_nicht_offen():
    """Sicherung gegen ein versehentliches OFFEN an der empfindlichsten
    Stelle — genau hier lag vor der Umstellung das Problem."""
    geld = [
        "api_buchhaltung", "api_buchhaltung_ausgaben",
        "api_angebote", "api_rechnungen", "api_belege_list",
        "api_erinnerung_senden", "api_rechnungen_pruefen",
    ]
    for name in geld:
        assert ROUTE_RECHTE.get(name, OFFEN) != OFFEN, (
            f"{name} steht auf OFFEN — damit saehe jeder Monteur wieder "
            f"die Umsaetze."
        )


def test_rechtevergabe_ist_gegen_sich_selbst_geschuetzt():
    """Wer Rechte vergeben darf, koennte sich alles geben. Diese drei
    Endpunkte muessen deshalb hinter dem nicht delegierbaren
    team.rechte haengen."""
    for name in ("api_team_rechte", "api_team_set_rolle", "api_team_set_recht"):
        assert ROUTE_RECHTE.get(name) == "team.rechte", name
    assert RECHTE["team.rechte"].nicht_delegierbar is True


@pytest.mark.parametrize("name", sorted(OEFFENTLICHE_ENDPUNKTE))
def test_nur_login_und_shell_sind_oeffentlich(name):
    """Die Liste der Endpunkte ohne Login soll klein und begruendet
    bleiben. Neue Eintraege muessen hier bewusst ergaenzt werden."""
    erlaubt = {
        "app_login_page", "app_login_request", "app_login_password",
        "app_login_consume", "app_activate_page", "app_activate_info",
        "app_activate_set", "app_manifest", "app_service_worker",
    }
    assert name in erlaubt, (
        f"{name} ist neu in OEFFENTLICHE_ENDPUNKTE. Ohne Login erreichbar "
        f"— ist das wirklich gewollt? Dann hier ergaenzen."
    )
