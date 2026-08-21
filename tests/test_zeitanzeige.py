"""Zeitanzeige in der App: UTC-Zeitstempel vs. Wanduhrzeit.

Hintergrund: die App zeigte Zeitstempel zwei Stunden zu frueh an (eine
um 08:52 ausgeloeste Bestellung stand als 06:52 im Verlauf). Die Werte
liegen korrekt als UTC in der DB, wurden aber ohne Umrechnung
formatiert.

Die Tuecke: es gibt zwei Sorten von Zeitstempeln, die im Modell gleich
aussehen. ``termin_datum`` traegt bewusst die lokale Wanduhrzeit mit
UTC-Etikett (siehe ``_kalender_termin_parsen``) — wuerde man die
mit-umrechnen, wanderte jeder 14-Uhr-Termin auf 16 Uhr.
"""
from __future__ import annotations

import datetime as dt

from core.api import app_screens


UTC = dt.timezone.utc


# =====================================================================
# _fmt_dt — echtes UTC, muss in Ortszeit umgerechnet werden
# =====================================================================

def test_utc_wird_im_sommer_zwei_stunden_vorgestellt():
    """CEST = UTC+2. Der Fall aus dem Bug-Report."""
    assert app_screens._fmt_dt(dt.datetime(2026, 8, 21, 6, 52, tzinfo=UTC)) == "21.08. 08:52"


def test_utc_wird_im_winter_eine_stunde_vorgestellt():
    """CET = UTC+1 — der Versatz ist keine feste Konstante."""
    assert app_screens._fmt_dt(dt.datetime(2026, 1, 15, 9, 30, tzinfo=UTC)) == "15.01. 10:30"


def test_utc_umrechnung_kann_den_tag_wechseln():
    """23:30 UTC ist im Sommer schon der naechste Tag."""
    assert app_screens._fmt_dt(dt.datetime(2026, 8, 21, 23, 30, tzinfo=UTC)) == "22.08. 01:30"


def test_naive_werte_bleiben_unangetastet():
    """Kalender-Events kommen naiv und sind schon Ortszeit."""
    assert app_screens._fmt_dt(dt.datetime(2026, 8, 21, 14, 0)) == "21.08. 14:00"


def test_leer_bleibt_leer():
    assert app_screens._fmt_dt(None) == ""
    assert app_screens._fmt_wanduhr(None) == ""


# =====================================================================
# _fmt_wanduhr — termin_datum, NICHT umrechnen
# =====================================================================

def test_wanduhr_ignoriert_das_utc_etikett():
    """Ein 14-Uhr-Termin bleibt 14 Uhr, auch mit tzinfo=UTC am Wert."""
    termin = dt.datetime(2026, 8, 21, 14, 0, tzinfo=UTC)
    assert app_screens._fmt_wanduhr(termin) == "21.08. 14:00"
    # Gegenprobe: die UTC-Variante wuerde ihn verschieben.
    assert app_screens._fmt_dt(termin) == "21.08. 16:00"


def test_wanduhr_auch_ohne_etikett():
    assert app_screens._fmt_wanduhr(dt.datetime(2026, 1, 15, 8, 5)) == "15.01. 08:05"


# =====================================================================
# Die Trennung muss an den Aufrufstellen eingehalten werden
# =====================================================================

def test_termin_datum_laeuft_nie_durch_die_utc_variante():
    """Schuetzt die Konvention: wer termin_datum anzeigt, nimmt
    _fmt_wanduhr. Sonst wandern Termine im Sommer zwei Stunden."""
    import inspect
    quelle = inspect.getsource(app_screens)
    treffer = [z.strip() for z in quelle.splitlines()
               if "_fmt_dt(" in z and "termin_datum" in z]
    assert treffer == [], f"termin_datum gehoert in _fmt_wanduhr: {treffer}"
