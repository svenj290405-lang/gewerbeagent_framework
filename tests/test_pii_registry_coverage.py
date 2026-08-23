"""Waechter ueber das PII-Verzeichnis (core/services/pii_registry.py).

Der Anlass: beim Audit am 2026-08-23 fehlten `kunden`, `rueckrufe` und
`wissensluecken.kunde` sowohl in der Auskunft (scripts/dsar.py) als auch
in den Loeschfristen (scripts/cleanup_pii.py). Solche Luecken tun nicht
weh — bis jemand eine Auskunft nach Art. 15 verlangt.

Deshalb dieser Meta-Test: er geht ueber ALLE Modelle und faellt rot,
sobald eine Tabelle personenbezogene Spalten hat, ueber die noch nie
jemand entschieden hat. Eine neue Tabelle mit `kunde_email` zwingt damit
zur Entscheidung, statt sie stillschweigend zu vergessen.
"""
from __future__ import annotations

import re
from pathlib import Path

import core.models  # noqa: F401  — registriert alle Modelle
from core.database.base import Base
from core.services import pii_registry as reg

REPO = Path(__file__).resolve().parent.parent

# Spaltennamen, die auf Personenbezug hindeuten. Bewusst breit: lieber
# einmal zu viel entscheiden als eine Tabelle uebersehen.
VERDAECHTIG = re.compile(
    r"(^|_)(email|mail|telefon|phone|name|adresse|address|anliegen|frage"
    r"|transcript|transkript|notiz|kunde)",
    re.IGNORECASE,
)

# Spalten, die den Verdacht nicht rechtfertigen (technische Felder).
UNVERDAECHTIG = {
    "mail_sent_at", "mail_message_id", "mail_internet_message_id",
    "mail_conversation_id", "mail_type", "tool_name", "kunde_id",
    "kunde_kontakt_id", "reschedule_mail_message_id",
    "reschedule_mail_conversation_id", "voice_phone_number",
}


def _tabellen_mit_pii() -> dict[str, list[str]]:
    treffer: dict[str, list[str]] = {}
    for name, tabelle in Base.metadata.tables.items():
        spalten = [
            c.name for c in tabelle.columns
            if VERDAECHTIG.search(c.name) and c.name not in UNVERDAECHTIG
        ]
        if spalten:
            treffer[name] = spalten
    return treffer


def test_jede_tabelle_mit_personenbezug_ist_entschieden():
    fehlend = {
        t: s for t, s in _tabellen_mit_pii().items() if t not in reg.REGISTRY
    }
    assert not fehlend, (
        "Diese Tabellen haben personenbezogene Spalten, stehen aber nicht in "
        "core/services/pii_registry.py. Bitte dort eintragen und entscheiden, "
        "ob sie geloescht, anonymisiert, nur gemeldet oder als "
        "Nicht-Endkundendaten eingestuft werden:\n  "
        + "\n  ".join(f"{t}: {s}" for t, s in sorted(fehlend.items()))
    )


def test_registry_kennt_keine_toten_tabellen():
    unbekannt = [t for t in reg.REGISTRY if t not in Base.metadata.tables]
    assert not unbekannt, (
        f"Registry nennt Tabellen, die es nicht (mehr) gibt: {unbekannt}"
    )


def test_umgangsarten_sind_gueltig():
    falsch = [
        e.tabelle for e in reg.REGISTRY.values()
        if e.umgang not in reg.ALLE_UMGANGSARTEN
    ]
    assert not falsch, f"Unbekannte Umgangsart bei: {falsch}"


def _modellname(tabelle: str) -> str | None:
    for mapper in Base.registry.mappers:
        if mapper.local_table is not None and mapper.local_table.name == tabelle:
            return mapper.class_.__name__
    return None


def test_dsar_behandelt_alles_was_als_behandelt_markiert_ist():
    """Was `in_dsar=True` sagt, muss in scripts/dsar.py auch vorkommen."""
    quelle = (REPO / "scripts" / "dsar.py").read_text(encoding="utf-8")
    fehlend = []
    for e in reg.REGISTRY.values():
        if not e.in_dsar:
            continue
        modell = _modellname(e.tabelle)
        # Wortgrenze, sonst passt "Kunde" auch auf "Kundengespraech" und
        # der Waechter waere blind.
        if modell and not re.search(rf"\b{modell}\b", quelle):
            fehlend.append(f"{e.tabelle} ({modell})")
    assert not fehlend, (
        "Als DSAR-relevant markiert, aber in scripts/dsar.py nicht "
        f"behandelt: {fehlend}"
    )


def test_cleanup_behandelt_alles_was_einer_frist_unterliegt():
    quelle = (REPO / "scripts" / "cleanup_pii.py").read_text(encoding="utf-8")
    fehlend = []
    for e in reg.REGISTRY.values():
        if not e.in_cleanup:
            continue
        modell = _modellname(e.tabelle)
        # Wortgrenze, sonst passt "Kunde" auch auf "Kundengespraech" und
        # der Waechter waere blind.
        if modell and not re.search(rf"\b{modell}\b", quelle):
            fehlend.append(f"{e.tabelle} ({modell})")
    assert not fehlend, (
        "Unterliegt laut Registry einer Loeschfrist, kommt in "
        f"scripts/cleanup_pii.py aber nicht vor: {fehlend}"
    )


def test_endkunden_tabellen_haben_matchspalten_oder_begruendung():
    """Wer gemeldet/geloescht wird, muss auffindbar sein."""
    ohne = [
        e.tabelle for e in reg.endkunden_tabellen()
        if not e.match_spalten and not e.hinweise
    ]
    assert not ohne, (
        "Ohne Match-Spalte findet die Auskunft die Zeilen nicht; entweder "
        f"Match-Spalten eintragen oder im Hinweis erklaeren: {ohne}"
    )
