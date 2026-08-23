"""Waechter ueber die Admin-Formulare.

Anlass: drei POST-Routen (`/tenants/{id}/retention`,
`.../features/{key}/toggle` und die neue Preis-Route) verlangten einen
Pflicht-Parameter `csrf_token` aus dem Formular. Die Templates schicken
das Feld aber seit dem CSRF-Fix im Mai unter dem Namen `_csrf` — und
FastAPI weist eine Anfrage mit fehlendem Pflichtfeld mit **422** ab,
bevor der Code ueberhaupt laeuft.

Folge: die DSGVO-Retention liess sich nicht mehr aendern und die
Feature-Schalter taten nichts. Beides sind Kernfunktionen des
Admin-Tools, und beides fiel nicht auf, weil die HTTP-Schicht
ungetestet war.

Geprueft wird deshalb genau die Falle: keine Admin-Route darf ein
Formularfeld verlangen, das die Templates nicht schicken.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

from fastapi import routing

from core.admin import routes as admin_routes

TEMPLATE_DIR = Path(admin_routes.__file__).resolve().parent / "templates"


def _admin_post_routen():
    for route in admin_routes.router.routes:
        if isinstance(route, routing.APIRoute) and "POST" in route.methods:
            yield route


def test_keine_route_verlangt_das_alte_csrf_feld():
    """`csrf_token` als Pflichtfeld = 422, weil die Formulare `_csrf` senden."""
    schuldige = []
    for route in _admin_post_routen():
        signatur = inspect.signature(route.endpoint)
        if "csrf_token" in signatur.parameters:
            schuldige.append(route.path)
    assert not schuldige, (
        "Diese Routen verlangen ein Formularfeld 'csrf_token'; die Templates "
        "senden aber '_csrf'. FastAPI antwortet dann mit 422, bevor der Code "
        f"laeuft — die Formulare sind damit tot: {schuldige}\n"
        "Die Pruefung macht `await require_csrf(request)` selbst."
    )


def test_alle_formular_felder_kommen_auch_im_template_vor():
    """Jedes Pflichtfeld einer POST-Route muss irgendein Template senden."""
    html = "\n".join(
        pfad.read_text(encoding="utf-8") for pfad in TEMPLATE_DIR.glob("*.html")
    )
    gesendet = set(re.findall(r'name="([^"]+)"', html))

    fehlend = []
    for route in _admin_post_routen():
        signatur = inspect.signature(route.endpoint)
        for name, param in signatur.parameters.items():
            # Nur echte Pflicht-Formularfelder interessieren.
            if param.default is inspect.Parameter.empty:
                continue
            if type(param.default).__name__ != "Form":
                continue
            if param.default.default is not ...:
                continue          # optionales Feld
            if name not in gesendet:
                fehlend.append(f"{route.path} verlangt '{name}'")
    assert not fehlend, (
        "Pflichtfelder, die kein Template sendet — die Route antwortet mit "
        f"422: {fehlend}"
    )


def test_jedes_formular_schickt_das_csrf_feld():
    """Ein Formular ohne `_csrf` wird von require_csrf abgewiesen."""
    # Login und Erst-Einrichtung laufen VOR der Sitzung — dort gibt es
    # noch kein Token, das man mitschicken koennte. Sie sind stattdessen
    # ueber das Login-Rate-Limit abgesichert.
    ohne_sitzung = {"login.html", "setup.html"}

    ohne = []
    for pfad in TEMPLATE_DIR.glob("*.html"):
        if pfad.name in ohne_sitzung:
            continue
        text = pfad.read_text(encoding="utf-8")
        for formular in re.findall(r"<form\b.*?</form>", text, re.S):
            if 'method="post"' not in formular.lower():
                continue
            if 'name="_csrf"' not in formular:
                ohne.append(pfad.name)
    assert not ohne, f"POST-Formulare ohne _csrf-Feld: {sorted(set(ohne))}"
