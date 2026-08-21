"""Feature-Catalog: alle Features die das System anbieten kann.

Diese Datei ist Single-Source-of-Truth. Wenn ein neues Feature ins
System kommt:
1. Eintrag in FEATURES hinzufuegen
2. ToolConfig.tool_name muss matchen mit Feature.key

Pro Tenant steuert `tool_configs.enabled` (tool_name == feature.key) ob
ein Feature aktiv ist — es gibt keine vordefinierten Pakete/Tiers mehr.
Jeder Tenant wird per Feature einzeln konfiguriert (Admin-UI bzw. das
Default-Set in scripts/onboard.py).

Die `key`-Werte sind die kanonischen tool_names die in
`tool_configs.tool_name` gespeichert sind.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Feature:
    """Beschreibung eines Features fuer Catalog + Admin-UI."""

    key: str
    """Kanonischer Name = ToolConfig.tool_name. snake_case, ASCII."""

    label: str
    """Anzeige-Name fuer Admin-UI."""

    description: str
    """1-Satz-Beschreibung fuer Admin-UI."""

    requires: tuple[str, ...] = ()
    """Features die vorher aktiv sein muessen (z.B. drive_archiv braucht kalender)."""

    always_on: bool = False
    """Wenn True: Feature kann nicht abgeschaltet werden.
    Erscheint im Admin-UI als 'immer aktiv', kein Toggle."""


# =====================================================================
# FEATURES — kanonische Liste
# =====================================================================

FEATURES: dict[str, Feature] = {
    # --- Basis-Tier ---
    "kalender": Feature(
        key="kalender",
        label="Kalender",
        description="Termine planen, Slot-Suche, Smart-Routing.",
    ),
    "wissensbasis": Feature(
        key="wissensbasis",
        label="Wissensbasis",
        description="Tenant-spezifisches Wissen (Leistungen, Anfahrt, FAQ).",
    ),

    # --- Pro-Tier ---
    "mail_intake": Feature(
        key="mail_intake",
        label="Mail-Inbox",
        description="Eingehende Anfragen automatisch lesen + beantworten.",
        requires=("kalender",),
    ),
    "anfrage_formular": Feature(
        key="anfrage_formular",
        label="Anfrage-Formular",
        description="Web-Formular fuer Kunden-Anfragen mit Datei-Upload.",
    ),
    "lexware": Feature(
        key="lexware",
        label="Buchhaltung",
        description="Belege erfassen, Rechnungen schreiben, Bezahlung tracken.",
    ),
    "material": Feature(
        key="material",
        label="Material-Bestellungen",
        description="Verbrauchsartikel-Katalog mit Quick-Order-Buttons.",
    ),
    # --- Enterprise-Tier ---
    # 'voice_init' matcht den existierenden tool_name (Plugin
    # voice_init bei ElevenLabs-Webhook).
    "voice_init": Feature(
        key="voice_init",
        label="Telefon-Annahme",
        description="KI-Telefonbeantworter mit Termin-Buchung im Anruf.",
        requires=("kalender",),
    ),
    "drive_archiv": Feature(
        key="drive_archiv",
        label="Kunden-Archiv",
        description="Bilder/PDFs pro Kunde in Drive-Ordnern archivieren.",
        requires=("kalender",),  # braucht Google-OAuth (kommt aus Kalender)
    ),
    "visualisierung": Feature(
        key="visualisierung",
        label="Visualisierung",
        description="Foto + Text-Beschreibung -> photorealistisches Rendering.",
    ),
    "objekt_suche": Feature(
        key="objekt_suche",
        label="Objekt erkennen",
        description=(
            "Foto von Geraet/Typenschild/Bauteil -> Modell bestimmen, in der "
            "Hersteller-Dokumentation nachschlagen, Bezugsquellen finden."
        ),
    ),
    "kunde_lookup": Feature(
        key="kunde_lookup",
        label="Kunden-Verlauf",
        description="Alle Gespraeche + Drive-Link pro Kunde anzeigen.",
        always_on=True,   # quasi gratis weil nur DB-Lookup
    ),
    "mitarbeiter": Feature(
        key="mitarbeiter",
        label="Mitarbeiter",
        description="Multi-Mitarbeiter mit eigenem Kalender + Skills.",
        requires=("kalender",),
    ),
    "werkstatt": Feature(
        key="werkstatt",
        label="Smart-Routing",
        description="Heimat-Adresse fuer Fahrtzeit-aware-Termin-Vorschlaege.",
        requires=("kalender",),
    ),
}


# =====================================================================
# Helpers
# =====================================================================


def all_known_feature_keys() -> frozenset[str]:
    """Alle bekannten Feature-Keys (FEATURES.keys + Feature.key)."""
    return frozenset(f.key for f in FEATURES.values())
