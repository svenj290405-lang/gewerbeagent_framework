"""Automatisierungsgrad: wie selbstaendig darf Q pro Funktion handeln?

Feature-Flags (``core/features/catalog.py``) beantworten die Frage *ob* eine
Funktion existiert. Diese Datei beantwortet die Frage *wie selbstaendig* Q sie
ausfuehren darf — pro Tenant einzeln einstellbar in der App unter
Einstellungen → Automatisierung.

Drei Stufen:

``manuell``
    Q handelt nicht. Im Chat wird das Tool Gemini gar nicht erst angeboten;
    Q sagt stattdessen, dass der Betrieb das selbst macht. Im Hintergrund
    (Mail, Telefon) laeuft nur noch die Benachrichtigung.

``assistiert``
    Q bereitet die Aktion vor und fragt vorher nach. Erst nach Freigabe wird
    ausgefuehrt. Das ist das heutige Verhalten im Q-Chat und deshalb der
    Default — nach dem Rollout aendert sich fuer bestehende Betriebe nichts.

``automatisch``
    Q fuehrt direkt aus und meldet es hinterher.

Nicht jede Automatisierung kann alle drei Stufen: eine Terminbuchung
mitten im Telefonat kann den Anrufer nicht warten lassen, bis der Chef
im Chat auf „Ja" tippt. Solche Automatisierungen listen in
``allowed_modes`` nur die Stufen, die sie wirklich koennen — die UI
zeigt die uebrigen ausgegraut mit ``unsupported_hint``.

Neue Automatisierung hinzufuegen:
1. Eintrag in AUTOMATIONS
2. ``tools`` auf die command_center-Write-Tools setzen, die dazugehoeren
3. Fuer Hintergrund-Automatisierungen ``tools=()`` lassen und die Stufe
   am Handlungspunkt selbst per ``mode_for_automation`` abfragen
"""
from __future__ import annotations

from dataclasses import dataclass

# =====================================================================
# Stufen
# =====================================================================

MODE_MANUELL = "manuell"
MODE_ASSISTIERT = "assistiert"
MODE_AUTOMATISCH = "automatisch"

ALL_MODES: tuple[str, ...] = (MODE_MANUELL, MODE_ASSISTIERT, MODE_AUTOMATISCH)

MODE_LABELS: dict[str, str] = {
    MODE_MANUELL: "Manuell",
    MODE_ASSISTIERT: "Assistiert",
    MODE_AUTOMATISCH: "Automatisch",
}

MODE_DESCRIPTIONS: dict[str, str] = {
    MODE_MANUELL: "Q macht es nicht — du machst es selbst.",
    MODE_ASSISTIERT: "Q bereitet vor und fragt vorher nach.",
    MODE_AUTOMATISCH: "Q macht es direkt und sagt dir Bescheid.",
}


@dataclass(frozen=True)
class Automation:
    """Eine Funktion, deren Selbstaendigkeit der Betrieb einstellen kann."""

    key: str
    """Kanonischer Name. snake_case, ASCII. Liegt so in der DB."""

    label: str
    """Anzeige-Name in der App."""

    description: str
    """1-Satz-Beschreibung fuer die Einstellungs-Seite."""

    tools: tuple[str, ...] = ()
    """command_center-Write-Tools, die diese Automatisierung steuert.
    Leer bei reinen Hintergrund-Automatisierungen (Mail-Cron, Telefon)."""

    feature: str | None = None
    """Feature-Key aus dem Catalog. Ist das Feature beim Tenant aus, taucht
    die Automatisierung in der App nicht auf."""

    allowed_modes: tuple[str, ...] = ALL_MODES
    """Welche Stufen diese Automatisierung wirklich beherrscht."""

    default_mode: str = MODE_ASSISTIERT
    """Stufe, solange der Tenant nichts eingestellt hat. Bewusst so
    gewaehlt, dass sie dem Verhalten VOR diesem Feature entspricht."""

    manuell_hint: str = ""
    """Was Q im Chat sagt, wenn die Stufe 'manuell' ist."""

    unsupported_hint: str = ""
    """Warum eine Stufe fehlt — erscheint als Hinweis unter dem Schalter."""

    group: str = "Assistent"
    """Ueberschrift, unter der die Automatisierung in der App steht."""


# =====================================================================
# AUTOMATIONS — kanonische Liste
# =====================================================================

_GRUPPE_ASSISTENT = "Q im Chat"
_GRUPPE_HINTERGRUND = "Ohne dich (Telefon & Mail)"


AUTOMATIONS: dict[str, Automation] = {
    # --- Q im Chat: alle drei Stufen moeglich ---
    "termin_buchen": Automation(
        key="termin_buchen",
        label="Termine buchen & verschieben",
        description="Q traegt Termine in den Kalender ein oder verschiebt sie.",
        tools=("termin_anlegen", "termin_verschieben"),
        feature="kalender",
        manuell_hint="Termine trägst du selbst ein — ich sage dir nur, was frei ist.",
        group=_GRUPPE_ASSISTENT,
    ),
    "termin_stornieren": Automation(
        key="termin_stornieren",
        label="Termine stornieren",
        description="Q sagt Termine ab und schickt dem Kunden die Storno-Mail.",
        tools=("termin_stornieren",),
        feature="kalender",
        manuell_hint="Absagen machst du selbst — ich suche dir den Termin nur heraus.",
        group=_GRUPPE_ASSISTENT,
    ),
    "mail_antwort": Automation(
        key="mail_antwort",
        label="Mails schreiben",
        description="Q schreibt und verschickt Mails an Kunden, die du beauftragst.",
        tools=("anfrage_beantworten", "email_schreiben"),
        manuell_hint="Mails schreibst du selbst — ich kann dir einen Text vorschlagen.",
        group=_GRUPPE_ASSISTENT,
    ),
    "angebot": Automation(
        key="angebot",
        label="Angebote",
        description="Q erstellt Angebote und verschickt sie an den Kunden.",
        tools=("angebot_erstellen", "angebot_senden"),
        feature="lexware",
        manuell_hint="Angebote machst du selbst — ich sammle dir die Daten zusammen.",
        group=_GRUPPE_ASSISTENT,
    ),
    "rechnung": Automation(
        key="rechnung",
        label="Rechnungen",
        description="Q erstellt Rechnungen und markiert sie als abgerechnet.",
        tools=("rechnung_erstellen", "rechnung_abrechnen"),
        feature="lexware",
        manuell_hint="Rechnungen schreibst du selbst — ich zeige dir nur, was offen ist.",
        group=_GRUPPE_ASSISTENT,
    ),
    "auftrag_status": Automation(
        key="auftrag_status",
        label="Auftrags-Status",
        description="Q schiebt Aufträge durch die Pipeline (Beratung → Rechnung).",
        tools=("auftrag_status",),
        feature="lexware",
        manuell_hint="Den Auftrags-Status setzt du selbst in der Auftrags-Ansicht.",
        group=_GRUPPE_ASSISTENT,
    ),
    "rueckruf": Automation(
        key="rueckruf",
        label="Rückrufe",
        description="Q legt Rückrufe an und hakt erledigte ab.",
        tools=("rueckruf_anlegen", "rueckruf_erledigt"),
        manuell_hint="Rückrufe pflegst du selbst — ich zeige dir nur die offenen.",
        group=_GRUPPE_ASSISTENT,
    ),
    "material": Automation(
        key="material",
        label="Material",
        description="Q bestellt Material und legt neue Artikel an.",
        tools=("material_bestellen", "material_anlegen"),
        manuell_hint="Material bestellst du selbst — ich sage dir nur, was im Katalog ist.",
        group=_GRUPPE_ASSISTENT,
    ),
    "wissen": Automation(
        key="wissen",
        label="Wissensbasis",
        description="Q merkt sich neue Betriebs-Infos, löscht veraltete und "
                    "beantwortet offene Kundenfragen aus der Lücken-Liste.",
        tools=("wissen_merken", "wissen_loeschen", "wissensluecke_beantworten"),
        feature="wissensbasis",
        manuell_hint="Die Wissensbasis pflegst du selbst unter „Mehr → Wissen\".",
        group=_GRUPPE_ASSISTENT,
    ),
    "team_abwesenheit": Automation(
        key="team_abwesenheit",
        label="Krank & Urlaub",
        description="Q meldet Mitarbeiter krank/im Urlaub und wieder zurück.",
        tools=("abwesenheit_melden", "mitarbeiter_zurueck"),
        feature="mitarbeiter",
        manuell_hint="Krank- und Urlaubsmeldungen trägst du selbst im Team-Bereich ein.",
        group=_GRUPPE_ASSISTENT,
    ),
    "archiv": Automation(
        key="archiv",
        label="Kunden-Archiv",
        description="Q legt Drive-Ordner und Notizen für Kunden an.",
        tools=("drive_ordner_anlegen", "drive_notiz_anlegen"),
        feature="drive_archiv",
        manuell_hint="Ordner und Notizen legst du selbst in Drive an.",
        group=_GRUPPE_ASSISTENT,
    ),

    # --- Hintergrund: laeuft ohne dass jemand in der App ist ---
    # 'assistiert' fehlt hier bewusst. Beides braeuchte eine Freigabe-
    # Schlange (Entwurf speichern → Push → Freigabe-Ansicht), die es noch
    # nicht gibt; beim Telefon waere sie ohnehin sinnlos, weil der Anrufer
    # in der Leitung haengt.
    "telefon_buchung": Automation(
        key="telefon_buchung",
        label="Terminbuchung am Telefon",
        description=(
            "Q bucht den Termin direkt im Anruf. Aus: Q nimmt das Anliegen "
            "auf und legt dir einen Rückruf an."
        ),
        feature="voice_init",
        allowed_modes=(MODE_MANUELL, MODE_AUTOMATISCH),
        default_mode=MODE_AUTOMATISCH,
        unsupported_hint=(
            "„Assistiert\" geht am Telefon nicht — der Anrufer kann nicht "
            "warten, bis du im Chat bestätigt hast."
        ),
        group=_GRUPPE_HINTERGRUND,
    ),
    "angebot_antwort": Automation(
        key="angebot_antwort",
        label="Angebots-Antworten erkennen",
        description=(
            "Q liest die Kundenantwort auf ein versandtes Angebot und erkennt "
            "Zusage oder Absage. Assistiert: Q schlägt dir vor, den Auftrag zu "
            "starten. Automatisch: Q setzt den Status direkt."
        ),
        feature="lexware",
        # Alle drei Stufen moeglich: anders als beim Mail-Auto-Antworten wird
        # hier NICHTS an den Kunden gesendet — assistiert heisst nur „Q meldet
        # dir den Vorschlag", die Freigabe ist der bestehende Status-Schalter
        # in der Auftrags-Ansicht. Darum braucht es keine Entwurfs-Schlange.
        allowed_modes=ALL_MODES,
        default_mode=MODE_ASSISTIERT,
        manuell_hint=(
            "Ob ein Kunde zugesagt hat, liest du selbst — ich fasse dir die "
            "Mail nur zusammen."
        ),
        group=_GRUPPE_HINTERGRUND,
    ),
    "mail_auto_antwort": Automation(
        key="mail_auto_antwort",
        label="Eingehende Mails beantworten",
        description=(
            "Q antwortet selbst auf Kundenanfragen im Postfach. Aus: Q liest "
            "mit und benachrichtigt dich, antwortet aber nicht."
        ),
        feature="mail_intake",
        allowed_modes=(MODE_MANUELL, MODE_AUTOMATISCH),
        default_mode=MODE_AUTOMATISCH,
        unsupported_hint=(
            "„Assistiert\" (Entwurf zur Freigabe) ist in Arbeit und noch "
            "nicht wählbar."
        ),
        group=_GRUPPE_HINTERGRUND,
    ),
}


# =====================================================================
# Helpers
# =====================================================================


def all_automation_keys() -> frozenset[str]:
    """Alle bekannten Automatisierungs-Keys."""
    return frozenset(AUTOMATIONS)


def is_valid_mode(automation_key: str, mode: str) -> bool:
    """Darf `automation_key` auf `mode` gestellt werden?

    Fail-closed: unbekannte Automatisierung oder unbekannte Stufe -> False.
    """
    auto = AUTOMATIONS.get(automation_key)
    if auto is None:
        return False
    return mode in auto.allowed_modes


def _build_tool_index() -> dict[str, str]:
    """Mapping: write-tool-name -> automation_key.

    Ein Tool darf nur zu genau einer Automatisierung gehoeren, sonst waere
    nicht entscheidbar, welche Stufe gilt. Doppelte Zuordnung ist ein Bug in
    der Registry und faellt im Test ``test_automations`` auf.
    """
    out: dict[str, str] = {}
    for auto in AUTOMATIONS.values():
        for tool in auto.tools:
            if tool in out:
                continue
            out[tool] = auto.key
    return out


# Beim Import einmal precomputed — read-only-Konstante.
TOOL_TO_AUTOMATION: dict[str, str] = _build_tool_index()


def automation_for_tool(tool_name: str) -> Automation | None:
    """Welche Automatisierung steuert dieses command_center-Tool?

    ``None`` = Tool ist nicht gesteuert und verhaelt sich wie bisher
    (Bestaetigung einholen). Das ist Absicht: ein neues Write-Tool ohne
    Registry-Eintrag darf nicht versehentlich vollautomatisch werden.
    """
    key = TOOL_TO_AUTOMATION.get(tool_name)
    return AUTOMATIONS.get(key) if key else None


def default_modes() -> dict[str, str]:
    """Alle Automatisierungen auf ihrer Default-Stufe."""
    return {k: a.default_mode for k, a in AUTOMATIONS.items()}
