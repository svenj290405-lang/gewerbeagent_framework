"""Rechte: wer in einem Betrieb was sehen und tun darf.

Die dritte Registry neben ``catalog.py`` und ``automations.py``, bewusst
nach demselben Muster gebaut:

    catalog.py      -> Was existiert bei diesem Betrieb?      (pro Tenant)
    automations.py  -> Wie selbstaendig darf Q das tun?       (pro Tenant)
    permissions.py  -> Wer im Betrieb darf es?                (pro Mitarbeiter)

Bis hierher gab es genau EIN Rechtebit: ``employees.is_default`` (Inhaber
ja/nein). Alles andere war "darf alles" — ein frisch angelegter Monteur
sah Umsaetze, offene Posten und die Nutzungsstatistik seiner Kollegen.

Aufbau
------
Drei Rollen-Vorlagen (``ROLLEN_PRESETS``) setzen den Standard. Abweichungen
davon speichert die Tabelle ``employee_permissions`` als einzelne Zeilen —
nur die Abweichung, nicht der ganze Satz. Damit erbt eine Buerokraft ein
spaeter hinzugefuegtes Recht automatisch in der Preset-Auspraegung, ohne
Backfill-Migration.

Zwei Regeln halten die Liste klein und sicher
---------------------------------------------
1. **Ein Key existiert nur, wenn mindestens ein Preset ihn verweigert.**
   Der Inhaber-Preset enthaelt per Konstruktion alle Keys. Das verhindert
   Zombie-Rechte wie "kunden.sehen", die nie jemand entzieht, und macht
   jeden neuen Key automatisch fail-closed. Als Test festgenagelt
   (tests/test_permissions.py).
2. **``nicht_delegierbar``** fuer Keys, mit denen man sich selbst
   hochstufen koennte. Ohne das koennte der Inhaber "Rechte vergeben" an
   eine Buerokraft geben, die sich anschliessend alles zuschaltet —
   Rechteausweitung ueber einen voellig legitimen Klick.

``is_default`` bleibt unangetastet. Es traegt drei Bedeutungen (Rolle,
Legacy-Mirror-Anker fuer die alten Tenant-Felder, Fallback-Empfaenger fuer
Notifications); abgeloest wird hier nur die erste.
"""
from __future__ import annotations

from dataclasses import dataclass

# =====================================================================
# Rollen
# =====================================================================

ROLLE_INHABER = "inhaber"
ROLLE_BUERO = "buero"
ROLLE_MONTEUR = "monteur"

ALLE_ROLLEN: tuple[str, ...] = (ROLLE_INHABER, ROLLE_BUERO, ROLLE_MONTEUR)

ROLLEN_LABELS: dict[str, str] = {
    ROLLE_INHABER: "Inhaber",
    ROLLE_BUERO: "Büro",
    ROLLE_MONTEUR: "Monteur",
}

ROLLEN_BESCHREIBUNG: dict[str, str] = {
    ROLLE_INHABER: "Sieht und darf alles, auch Geld und Einstellungen.",
    ROLLE_BUERO: "Organisiert den Betrieb — ohne Buchhaltung und Einstellungen.",
    ROLLE_MONTEUR: "Arbeitet auf der Baustelle: eigene Aufträge, Termine, Material.",
}

# Restriktivste Rolle — Default fuer neue Mitarbeiter und Rueckfall bei
# einem unbekannten Wert in der Spalte.
ROLLE_DEFAULT = ROLLE_MONTEUR


# =====================================================================
# Rechte
# =====================================================================

@dataclass(frozen=True)
class Recht:
    """Ein Recht, wie es die Rechte-Oberflaeche dem Inhaber anzeigt."""

    key: str
    label: str
    description: str
    gruppe: str
    # Kann NICHT per Override an einen Nicht-Inhaber vergeben werden.
    # Schutz gegen Rechteausweitung (siehe Modul-Docstring).
    nicht_delegierbar: bool = False


GRUPPE_GELD = "Geld"
GRUPPE_AUFTRAEGE = "Aufträge"
GRUPPE_KUNDEN = "Kunden & Anfragen"
GRUPPE_BETRIEB = "Betrieb"


RECHTE: dict[str, Recht] = {
    # ---- Geld ----------------------------------------------------
    "buchhaltung.sehen": Recht(
        key="buchhaltung.sehen",
        label="Buchhaltung sehen",
        description=(
            "Umsätze, offene Posten, Angebote, Rechnungen und Belege "
            "einsehen."
        ),
        gruppe=GRUPPE_GELD,
    ),
    "buchhaltung.fuehren": Recht(
        key="buchhaltung.fuehren",
        label="Angebote & Rechnungen schreiben",
        description=(
            "Angebote und Rechnungen anlegen und versenden, Zahlungs"
            "erinnerungen schicken, Zahlungen abgleichen, Belege kontieren."
        ),
        gruppe=GRUPPE_GELD,
    ),

    # ---- Aufträge ------------------------------------------------
    "auftraege.alle_sehen": Recht(
        key="auftraege.alle_sehen",
        label="Alle Aufträge sehen",
        description=(
            "Ohne dieses Recht sieht jemand nur die Aufträge, die ihm "
            "zugewiesen sind."
        ),
        gruppe=GRUPPE_AUFTRAEGE,
    ),
    "auftraege.fuehren": Recht(
        key="auftraege.fuehren",
        label="Aufträge anlegen und steuern",
        description=(
            "Neue Aufträge anlegen, Status ändern, jemandem zuweisen und "
            "den Auftragsablauf des Betriebs festlegen."
        ),
        gruppe=GRUPPE_AUFTRAEGE,
    ),

    # ---- Kunden & Anfragen ---------------------------------------
    "kunden.pflegen": Recht(
        key="kunden.pflegen",
        label="Kundendaten pflegen",
        description="Kunden zusammenführen und Gespräche einem Kunden zuordnen.",
        gruppe=GRUPPE_KUNDEN,
    ),
    "anfragen.bearbeiten": Recht(
        key="anfragen.bearbeiten",
        label="Auf Anfragen antworten",
        description="Kundenanfragen aus dem Postfach im Namen des Betriebs beantworten.",
        gruppe=GRUPPE_KUNDEN,
    ),

    # ---- Betrieb -------------------------------------------------
    "material.verwalten": Recht(
        key="material.verwalten",
        label="Materialkatalog pflegen",
        description=(
            "Artikel anlegen und ausblenden. Bestellen darf jeder auch ohne "
            "dieses Recht."
        ),
        gruppe=GRUPPE_BETRIEB,
    ),
    "wissen.pflegen": Recht(
        key="wissen.pflegen",
        label="Wissensdatenbank pflegen",
        description="Einträge anlegen und löschen, die Q in Gesprächen nutzt.",
        gruppe=GRUPPE_BETRIEB,
    ),
    "team.sehen": Recht(
        key="team.sehen",
        label="Team-Übersicht sehen",
        description=(
            "Die Mitarbeiterliste mit Abwesenheiten und App-Nutzung der "
            "Kollegen."
        ),
        gruppe=GRUPPE_BETRIEB,
    ),
    "team.fuehren": Recht(
        key="team.fuehren",
        label="Team verwalten",
        description=(
            "Mitarbeiter anlegen, aktivieren, Profil pflegen, krank oder "
            "im Urlaub melden."
        ),
        gruppe=GRUPPE_BETRIEB,
    ),
    "team.rechte": Recht(
        key="team.rechte",
        label="Rechte vergeben",
        description="Rollen und einzelne Rechte der Mitarbeiter ändern.",
        gruppe=GRUPPE_BETRIEB,
        nicht_delegierbar=True,
    ),
    "einstellungen.verwalten": Recht(
        key="einstellungen.verwalten",
        label="Einstellungen & Verbindungen",
        description=(
            "Stammdaten, Automatisierungsgrade, Erscheinungsbild, "
            "Anfrage-Formulare sowie Google-, Outlook- und Lexware-"
            "Verbindungen des Betriebs."
        ),
        gruppe=GRUPPE_BETRIEB,
        nicht_delegierbar=True,
    ),
}


ALLE_RECHTE: frozenset[str] = frozenset(RECHTE)


# =====================================================================
# Rollen-Vorlagen
# =====================================================================
#
# Der Inhaber bekommt per Konstruktion alles. Bei den anderen beiden
# steht bewusst da, was sie DUERFEN — eine Verbotsliste veraltet, sobald
# ein Key dazukommt (und waere dann fail-open).

ROLLEN_PRESETS: dict[str, frozenset[str]] = {
    ROLLE_INHABER: ALLE_RECHTE,
    ROLLE_BUERO: frozenset({
        "auftraege.alle_sehen",
        "auftraege.fuehren",
        "kunden.pflegen",
        "anfragen.bearbeiten",
        "material.verwalten",
        "wissen.pflegen",
        "team.sehen",
    }),
    # Monteur: alles, was kein eigenes Recht braucht (Termine, Kunden
    # nachschlagen, Material bestellen, Stunden buchen, Belege
    # hochladen, Q nutzen) — plus nichts darueber hinaus.
    ROLLE_MONTEUR: frozenset(),
}


def rechte_fuer_rolle(rolle: str | None) -> frozenset[str]:
    """Preset einer Rolle. Unbekannt/None -> restriktivste Rolle."""
    return ROLLEN_PRESETS.get(rolle or "", ROLLEN_PRESETS[ROLLE_DEFAULT])


def ist_delegierbar(key: str) -> bool:
    """Darf dieses Recht per Override an einen Nicht-Inhaber gehen?"""
    recht = RECHTE.get(key)
    return recht is not None and not recht.nicht_delegierbar


def ist_gueltige_rolle(rolle: str) -> bool:
    return rolle in ALLE_ROLLEN


def rechte_nach_gruppe() -> dict[str, list[Recht]]:
    """Rechte gruppiert, in Registry-Reihenfolge — fuer die Rechte-UI."""
    out: dict[str, list[Recht]] = {}
    for recht in RECHTE.values():
        out.setdefault(recht.gruppe, []).append(recht)
    return out
