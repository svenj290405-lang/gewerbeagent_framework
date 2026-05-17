"""Telefonnummer-Normalisierung fuer Storno-Suche und Event-Metadaten.

Wird vom kalender-Plugin genutzt um Telefonnummern in einer stabilen
Form als extendedProperty am Kalender-Event abzulegen — und um beim
spaeteren Find-Events-Lookup verschiedene Schreibweisen derselben
Nummer zuverlaessig zu matchen.

Hintergrund: ueber die Voice-Pipeline kommen Telefonnummern in
verschiedensten Formaten an ("+49 30 1234", "0030 1234", "030/1234-56",
"+49(0)30 1234"). Ein roher String-Vergleich findet die Nummer nicht
wieder. Wir normalisieren auf eine ziffern-only Country-Form und
matchen via Suffix (letzte 8 Ziffern) als robusten Fallback.
"""
from __future__ import annotations


def normalize_phone(raw: str | None) -> str:
    """Normalisiert eine Telefonnummer zu einem stabilen Vergleichs-Key.

    Regeln (DE-zentriert — Gewerbeagent-Tenants sitzen in DE):
    - alle Nicht-Ziffern raus (`+`, Leerzeichen, `/`, `-`, `(`, `)`)
    - `00`-Prefix als Auslands-Vorwahl entfernen (`00491...` -> `491...`)
    - `490`-Sequenz: die 0 nach Country-Code 49 entfernen
      (`+49 (0)30 1234` -> `4903012345` -> `493012345`)

    Leerer/None-Input liefert Leerstring. Wir machen kein Country-Code-
    Erraten fuer nationale Nummern ohne Prefix ("030 1234" bleibt
    "0301234") — der Suffix-Match in `phone_match_key` faengt das ab.

    Returns: Ziffern-only String, evtl. leer.
    """
    if not raw:
        return ""
    digits = "".join(c for c in raw if c.isdigit())
    if not digits:
        return ""
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("490"):
        digits = "49" + digits[3:]
    return digits


def phone_match_key(normalized: str) -> str:
    """Liefert die letzten 8 Ziffern als robusten Match-Key.

    Wird genutzt fuer Suffix-Matching wenn zwei Nummern in
    unterschiedlicher Country-Form vorliegen ("4930123456" vs
    "0301234567" — beide enden auf "01234567" wenn die nationale
    Nummer 8+ Ziffern hat).

    Bei < 8 Ziffern: gibt den ganzen String zurueck (Match wird
    dann tendenziell weniger zuverlaessig — der Caller sollte das
    Ergebnis vor Vertrauen quer-pruefen, z.B. via Event-Datum).
    """
    return normalized[-8:] if len(normalized) >= 8 else normalized
