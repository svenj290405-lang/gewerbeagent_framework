"""Feature-Catalog: alle Features die das System anbieten kann.

Diese Datei ist Single-Source-of-Truth. Wenn ein neues Feature ins
System kommt:
1. Eintrag in FEATURES hinzufuegen
2. ToolConfig.tool_name muss matchen mit Feature.key
3. Wenn Telegram-Befehle dazugehoeren: in `telegram_commands` listen
   damit /help sie nur fuer Tenants mit dem Feature anzeigt

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
        telegram_commands=("/start", "/help", "/status", "/abbrechen"),
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
        telegram_commands=(
            "/formular", "/formular_anzeigen", "/formular_zuruecksetzen",
            "/formulare", "/formulare_offen",
        ),
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
        telegram_commands=("/drive_verbinden", "/drive_status", "/drive", "/archiv", "/fertig"),
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
# Helpers
# =====================================================================


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
