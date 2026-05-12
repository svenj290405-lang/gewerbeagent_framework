"""Feature-Catalog: alle Features + die 3 vordefinierten Pakete.

Diese Datei ist Single-Source-of-Truth. Wenn ein neues Feature ins
System kommt:
1. Eintrag in FEATURES hinzufuegen
2. Eintrag in PACKAGES je nach Tier (Basis/Pro/Enterprise)
3. ToolConfig.tool_name muss matchen mit Feature.key
4. Wenn Telegram-Befehle dazugehoeren: in `telegram_commands` listen
   damit /help sie nur fuer Tenants mit dem Feature anzeigt

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
    """Anzeige-Name fuer Admin-UI + /paket-Befehl."""

    description: str
    """1-Satz-Beschreibung fuer Admin-UI."""

    requires: tuple[str, ...] = ()
    """Features die vorher aktiv sein muessen (z.B. drive_archiv braucht kalender)."""

    telegram_commands: tuple[str, ...] = ()
    """Telegram-Befehle die nur verfuegbar sind wenn Feature aktiv ist.
    Werden von /help-Filter genutzt + Feature-Gate-Dispatcher."""

    always_on: bool = False
    """Wenn True: Feature kann nicht abgeschaltet werden (z.B. /help, /start).
    Erscheint im Admin-UI als 'immer aktiv', kein Toggle."""


# =====================================================================
# FEATURES — kanonische Liste
# =====================================================================

FEATURES: dict[str, Feature] = {
    # --- Always-on (Bot-Grundfunktion) ---
    "telegram_bot": Feature(
        key="telegram_bot",
        label="Telegram-Bot",
        description="Grundlegende Bot-Verbindung. Ohne dies geht nichts.",
        always_on=True,
        telegram_commands=("/start", "/help", "/status", "/abbrechen", "/paket"),
    ),

    # --- Basis-Tier ---
    "kalender": Feature(
        key="kalender",
        label="Kalender",
        description="Termine planen, Slot-Suche, Smart-Routing.",
        telegram_commands=("/kalender_verbinden", "/kalender_status", "/briefing"),
    ),
    "wissensbasis": Feature(
        key="wissensbasis",
        label="Wissensbasis",
        description="Tenant-spezifisches Wissen (Leistungen, Anfahrt, FAQ).",
        telegram_commands=("/wissen", "/wissen_anzeigen", "/wissen_loeschen"),
    ),

    # --- Pro-Tier ---
    "mail_intake": Feature(
        key="mail_intake",
        label="Mail-Inbox",
        description="Eingehende Anfragen automatisch lesen + beantworten.",
        requires=("kalender",),
        telegram_commands=("/microsoft_setup", "/microsoft_status", "/microsoft_check"),
    ),
    "anfrage_formular": Feature(
        key="anfrage_formular",
        label="Anfrage-Formular",
        description="Web-Formular fuer Kunden-Anfragen mit Datei-Upload.",
        telegram_commands=("/formular", "/formular_anzeigen", "/formular_zuruecksetzen"),
    ),
    "lexware": Feature(
        key="lexware",
        label="Buchhaltung",
        description="Belege erfassen, Rechnungen schreiben, Bezahlung tracken.",
        telegram_commands=(
            "/lexware_setup", "/lexware_status",
            "/beleg", "/belege_anzeigen",
            "/rechnung", "/rechnungen_anzeigen", "/rechnung_pruefen",
            "/angebot",
            "/auftraege", "/auftrag",
        ),
    ),
    "material": Feature(
        key="material",
        label="Material-Bestellungen",
        description="Verbrauchsartikel-Katalog mit Quick-Order-Buttons.",
        telegram_commands=(
            "/material", "/material_neu",
        ),
    ),
    "kalkulation": Feature(
        key="kalkulation",
        label="Kalkulations-Engine",
        description="Mathematische Formeln fuer Angebots-Erstellung.",
        telegram_commands=(
            "/kalkulation", "/kalkulation_anzeigen",
            "/kalkulation_loeschen", "/kalkulation_excel",
        ),
    ),

    # --- Enterprise-Tier ---
    # 'voice_init' matcht den existierenden tool_name (Plugin
    # voice_init bei ElevenLabs-Webhook).
    "voice_init": Feature(
        key="voice_init",
        label="Telefon-Annahme",
        description="KI-Telefonbeantworter mit Termin-Buchung im Anruf.",
        requires=("kalender",),
        telegram_commands=("/aufnahme", "/anrufe"),
    ),
    "drive_archiv": Feature(
        key="drive_archiv",
        label="Kunden-Archiv",
        description="Bilder/PDFs pro Kunde in Drive-Ordnern archivieren.",
        requires=("kalender",),  # braucht Google-OAuth (kommt aus Kalender)
        telegram_commands=("/drive_verbinden", "/drive_status", "/archiv", "/fertig"),
    ),
    "visualisierung": Feature(
        key="visualisierung",
        label="Visualisierung",
        description="Foto + Text-Beschreibung -> photorealistisches Rendering.",
        telegram_commands=("/visualisierung",),
    ),
    "kunde_lookup": Feature(
        key="kunde_lookup",
        label="Kunden-Verlauf",
        description="Alle Gespraeche + Drive-Link pro Kunde anzeigen.",
        always_on=True,   # quasi gratis weil nur DB-Lookup
        telegram_commands=("/kunde",),
    ),
    "mitarbeiter": Feature(
        key="mitarbeiter",
        label="Mitarbeiter",
        description="Multi-Mitarbeiter mit eigenem Kalender + Skills.",
        requires=("kalender",),
        telegram_commands=(
            "/mitarbeiter",
            "/team",
            "/krank",
            "/urlaub",
            "/abwesend",
            "/zurueck",
        ),
    ),
    "werkstatt": Feature(
        key="werkstatt",
        label="Smart-Routing",
        description="Heimat-Adresse fuer Fahrtzeit-aware-Termin-Vorschlaege.",
        requires=("kalender",),
        telegram_commands=("/werkstatt", "/werkstatt_status"),
    ),
}


# =====================================================================
# PACKAGES
# =====================================================================

PACKAGE_BASIS = "basis"
PACKAGE_PRO = "pro"
PACKAGE_ENTERPRISE = "enterprise"
PACKAGE_CUSTOM = "custom"

ALL_PACKAGES = (PACKAGE_BASIS, PACKAGE_PRO, PACKAGE_ENTERPRISE, PACKAGE_CUSTOM)


# Paket-Definitionen — frozenset damit Vergleiche stabil sind.
# 'telegram_bot' und 'kunde_lookup' (always_on=True) sind nicht extra
# gelistet — sie sind in jedem Paket aktiv per Definition.
PACKAGES: dict[str, frozenset[str]] = {
    PACKAGE_BASIS: frozenset({
        "kalender",
        "wissensbasis",
    }),
    PACKAGE_PRO: frozenset({
        "kalender",
        "wissensbasis",
        "mail_intake",
        "anfrage_formular",
        "lexware",
        "material",
        "kalkulation",
        "werkstatt",
    }),
    PACKAGE_ENTERPRISE: frozenset({
        "kalender",
        "wissensbasis",
        "mail_intake",
        "anfrage_formular",
        "lexware",
        "material",
        "kalkulation",
        "werkstatt",
        "voice_init",
        "drive_archiv",
        "visualisierung",
        "mitarbeiter",
    }),
    # PACKAGE_CUSTOM hat keine feste Liste — Sven setzt manuell.
}


# Anzeige-Reihenfolge in /paket + Admin-UI (top-down).
PACKAGE_DISPLAY_ORDER: dict[str, int] = {
    PACKAGE_BASIS: 1,
    PACKAGE_PRO: 2,
    PACKAGE_ENTERPRISE: 3,
    PACKAGE_CUSTOM: 4,
}


PACKAGE_LABELS: dict[str, str] = {
    PACKAGE_BASIS: "📦 Basis",
    PACKAGE_PRO: "📦 Pro",
    PACKAGE_ENTERPRISE: "📦 Enterprise",
    PACKAGE_CUSTOM: "🛠 Custom",
}


def features_in_package(package: str) -> frozenset[str]:
    """Liefert die Feature-Keys eines Pakets inkl. always_on-Features.

    PACKAGE_CUSTOM liefert leeres Set — Caller muss apply_package nicht
    aufrufen sondern Features einzeln togglen.
    """
    if package == PACKAGE_CUSTOM:
        return frozenset()
    base = PACKAGES.get(package, frozenset())
    always_on = frozenset(
        f.key for f in FEATURES.values() if f.always_on
    )
    return base | always_on


def all_known_feature_keys() -> frozenset[str]:
    """Alle bekannten Feature-Keys (FEATURES.keys + Feature.key)."""
    return frozenset(f.key for f in FEATURES.values())


def telegram_command_to_feature() -> dict[str, str]:
    """Mapping: '/befehl' -> feature_key.

    Wird vom Telegram-Dispatcher genutzt um vor jedem Command zu pruefen
    ob das Feature aktiv ist. Built nur einmal beim Import — keine
    Runtime-Kosten.
    """
    out: dict[str, str] = {}
    for f in FEATURES.values():
        for cmd in f.telegram_commands:
            # Erstes Match gewinnt; wenn Befehl in mehreren Features
            # vorkommt, ist die Feature-Definition kaputt → wir loggen.
            if cmd in out:
                # Konflikt: derselbe Befehl unter zwei Features. Nicht
                # crashen, aber im Test sichtbar machen.
                continue
            out[cmd] = f.key
    return out


# Beim Import einmal precomputed — read-only-Konstante.
COMMAND_TO_FEATURE = telegram_command_to_feature()
