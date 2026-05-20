"""Reply-Trimming: schneidet den zitierten Original-Block aus einer
eingehenden Mail, sodass nur der NEUE Teil (oben) fuer LLM-Klassifikation
+ Intent-Erkennung genutzt wird.

Erkannte Quote-Marker (Schnitt ab erster Fundstelle):
  - GMX/Thunderbird:  "Am 18.05.26 um 14:30 schrieb Max <…>:"
  - Gmail/Apple (en): "On Mon, 18 May 2026 at 14:30 Max <…> wrote:"
  - Outlook:          Header-Block "Von:/From:" (+ Gesendet/An/Betreff…)
                      bzw. "-----Urspruengliche Nachricht-----" / Divider
  - Plain quoting:    Zeilen die mit ">" beginnen

Der eigentliche Mail-Body bleibt unangetastet (nur die LLM-Eingabe wird
getrimmt) — das Original bleibt fuer Threading/Archiv erhalten.
"""
from __future__ import annotations

import re

# Attributions-/Trenn-Zeilen, ab denen zitiert wird (eine ganze Zeile).
_MARKERS = [
    # "Am <datum> [um <zeit>] schrieb <name>:" (GMX, Thunderbird, Web.de)
    re.compile(r"^\s*Am\s.+\sschrieb\s.+:\s*$", re.IGNORECASE),
    # "On <datum> <name> wrote:" (Gmail, Apple Mail, en)
    re.compile(r"^\s*On\s.+\swrote:\s*$", re.IGNORECASE),
    # "----- Urspruengliche Nachricht -----" / "----- Original Message -----"
    re.compile(r"^\s*-{2,}\s*(Urspr\w*\s+Nachricht|Original[- ]?Nachricht|Original Message)\s*-{2,}\s*$", re.IGNORECASE),
]

# Outlook-Divider: lange Unterstrich-Linie
_DIVIDER = re.compile(r"^\s*_{10,}\s*$")

# Outlook-Header-Block: "Von:"/"From:" nur als Quote werten, wenn kurz
# darunter ein typischer Header folgt (sonst Fehlalarm bei "Von mir aus…").
_VON = re.compile(r"^\s*(Von|From):\s*\S.*$", re.IGNORECASE)
_HEADER_AFTER_VON = re.compile(
    r"^\s*(Gesendet|Sent|An|To|Betreff|Subject|Cc|Datum|Date):", re.IGNORECASE,
)


def trim_quoted_reply(text: str) -> str:
    """Gibt nur den neuen Teil oberhalb des ersten Quote-Markers zurueck.

    Kein Marker gefunden -> Text unveraendert (gestrippt). Bleibt nach dem
    Schnitt nichts uebrig (Mail bestand nur aus Zitat) -> Originaltext
    behalten, damit nicht leer klassifiziert wird.
    """
    if not text:
        return text or ""
    lines = text.split("\n")
    cut: int | None = None
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(">"):
            cut = i
            break
        if _DIVIDER.match(line):
            cut = i
            break
        if any(rx.match(line) for rx in _MARKERS):
            cut = i
            break
        if _VON.match(line):
            lookahead = lines[i + 1:i + 6]
            if any(_HEADER_AFTER_VON.match(l) for l in lookahead):
                cut = i
                break
    if cut is None:
        return text.strip()
    new = "\n".join(lines[:cut]).strip()
    return new if new else text.strip()
