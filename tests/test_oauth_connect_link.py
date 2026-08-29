"""Der oeffentliche OAuth-Einstieg braucht einen signierten Link.

`_reject_hijack` schuetzt bewusst erst ab der ZWEITEN Verbindung — die
erste war damit fuer jeden offen, der einen Tenant-Slug erraet. Wer das
tat, haengte sein eigenes Google-/Outlook-Konto an einen fremden Betrieb:
dessen Kundenmails gingen danach ueber sein Postfach raus, dessen
Kundenfotos in sein Drive.
"""
from __future__ import annotations

import time

import pytest

from core.security.oauth_flow import (
    _connect_signatur,
    connect_link_gueltig,
    make_connect_link,
)


def test_frischer_link_ist_gueltig():
    link = make_connect_link("pilot", "google", "default")
    from urllib.parse import parse_qs, urlparse

    q = parse_qs(urlparse(link).query)
    assert connect_link_gueltig(
        "pilot", "google", "default", q["exp"][0], q["sig"][0],
    )


def test_ohne_signatur_ungueltig():
    assert not connect_link_gueltig("pilot", "google", None, int(time.time()) + 60, None)
    assert not connect_link_gueltig("pilot", "google", None, None, "abc")


def test_signatur_gilt_nicht_fuer_einen_anderen_betrieb():
    """Der Kern: ein Link fuer den eigenen Betrieb oeffnet keinen fremden."""
    exp = int(time.time()) + 600
    sig = _connect_signatur("meinbetrieb", "google", None, exp)
    assert not connect_link_gueltig("fremderbetrieb", "google", None, exp, sig)


def test_signatur_gilt_nicht_fuer_anderen_provider_oder_mitarbeiter():
    exp = int(time.time()) + 600
    sig = _connect_signatur("pilot", "google", "default", exp)
    assert not connect_link_gueltig("pilot", "microsoft", "default", exp, sig)
    assert not connect_link_gueltig("pilot", "google", "henrik", exp, sig)


def test_abgelaufener_link_ungueltig():
    exp = int(time.time()) - 1
    sig = _connect_signatur("pilot", "google", None, exp)
    assert not connect_link_gueltig("pilot", "google", None, exp, sig)


def test_kaputtes_exp_faellt_geschlossen_aus():
    sig = _connect_signatur("pilot", "google", None, 1)
    assert not connect_link_gueltig("pilot", "google", None, "morgen", sig)


@pytest.mark.parametrize("query", [
    "tenant=pilot&provider=google",                       # gar nicht signiert
    "tenant=pilot&provider=google&exp=99999999999&sig=x",  # falsch signiert
])
def test_route_lehnt_unsignierte_starts_ab(query):
    """Gleiche Antwort fuer alle Ablehnungsgruende — sonst waere die Route
    ein Orakel dafuer, welche Slugs es gibt."""
    from fastapi.testclient import TestClient

    from core.api.app import app

    with TestClient(app) as client:
        r = client.get(f"/oauth/start?{query}", follow_redirects=False)
    assert r.status_code == 404


def test_reject_hijack_zaehlt_die_zeile_nicht_den_token():
    """Ein abgebrochener Flow hinterlaesst eine Zeile mit leerem Token.

    Die alte Pruefung (`refresh_token` nicht leer) hielt das fuer eine
    Erst-Verbindung und liess genau den Wechsel durch, den sie verhindern
    soll.
    """
    import inspect

    from core.security import oauth_flow

    quelle = inspect.getsource(oauth_flow._reject_hijack)
    assert "persistent" in quelle
    assert 'bool((oauth_token.refresh_token or "").strip())' not in quelle
