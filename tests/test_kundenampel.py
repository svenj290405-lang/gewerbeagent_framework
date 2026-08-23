"""Tests fuer die Kundenampel im Admin.

Die Ampel entscheidet, wo der Betreiber morgens hinschaut. Deshalb muss
die Bewertung nachvollziehbar sein — und vor allem: eine tote Verbindung
darf nicht als "laeuft" durchgehen, und ein Betrieb ohne Auffaelligkeit
darf nicht grundlos rot werden.
"""
from __future__ import annotations

from core.services import kundenampel as ka


def test_tote_verbindung_ist_rot():
    farbe, grund = ka._bewerte(
        {"microsoft": {"ok": False, "fehler": "401"},
         "google": {"ok": True}},
        nutzung_7t=20,
        offen={"offene_anfragen": 0, "offene_rueckrufe": 0, "haengende_mails": 0},
    )
    assert farbe == ka.ROT
    assert "microsoft" in grund


def test_ablaufender_zugang_ist_gelb_nicht_rot():
    """Noch funktioniert er — aber es eilt."""
    farbe, grund = ka._bewerte(
        {"microsoft": {"ok": False, "tage_ohne_nutzung": 75}},
        nutzung_7t=5,
        offen={"offene_anfragen": 0, "offene_rueckrufe": 0, "haengende_mails": 0},
    )
    assert farbe == ka.GELB
    assert "75" in grund


def test_stiller_betrieb_ist_gelb():
    """Kein Login in einer Woche heisst: da springt jemand ab."""
    farbe, grund = ka._bewerte(
        {"microsoft": {"ok": True}},
        nutzung_7t=0,
        offen={"offene_anfragen": 0, "offene_rueckrufe": 0, "haengende_mails": 0},
    )
    assert farbe == ka.GELB
    assert "Nutzung" in grund


def test_haengende_mails_sind_gelb():
    farbe, grund = ka._bewerte(
        {"google": {"ok": True}},
        nutzung_7t=12,
        offen={"offene_anfragen": 0, "offene_rueckrufe": 0, "haengende_mails": 3},
    )
    assert farbe == ka.GELB
    assert "3" in grund


def test_offener_rueckruf_ist_kein_alarm():
    """Rueckrufe sind Alltag des Betriebs, kein Systemproblem."""
    farbe, grund = ka._bewerte(
        {"google": {"ok": True}},
        nutzung_7t=12,
        offen={"offene_anfragen": 2, "offene_rueckrufe": 1, "haengende_mails": 0},
    )
    assert farbe == ka.GRUEN
    assert "Rueckruf" in grund


def test_ohne_anbindung_gelb_statt_gruen():
    """Ein Betrieb ganz ohne Verbindung ist nicht fertig eingerichtet."""
    farbe, grund = ka._bewerte(
        {}, nutzung_7t=3,
        offen={"offene_anfragen": 0, "offene_rueckrufe": 0, "haengende_mails": 0},
    )
    assert farbe == ka.GELB
    assert "Einrichtung" in grund


def test_alles_in_ordnung_ist_gruen():
    farbe, grund = ka._bewerte(
        {"microsoft": {"ok": True}, "lexware": {"ok": True}},
        nutzung_7t=42,
        offen={"offene_anfragen": 0, "offene_rueckrufe": 0, "haengende_mails": 0},
    )
    assert farbe == ka.GRUEN
    assert "42" in grund
