"""Verzeichnis aller Tabellen mit Personenbezug — eine Quelle, zwei Nutzer.

Warum es das gibt: `scripts/dsar.py` (Auskunft und Loeschung auf Verlangen)
und `scripts/cleanup_pii.py` (automatische Loeschfristen) pflegten ihre
Tabellenliste jeweils von Hand. Beim Audit am 2026-08-23 fiel auf, dass
gleich drei Tabellen in beiden fehlten: die Kunden-Stammtabelle `kunden`,
die Rueckrufe (Name, Telefon, Anliegen) und das `kunde`-Feld der
Wissensluecken. Eine Auskunft nach Art. 15 war damit unvollstaendig und
eine Loeschung nach Art. 17 gar nicht moeglich — und niemandem faellt so
etwas auf, weil beim Weglassen nichts kaputtgeht.

Dieses Modul haelt die Entscheidung pro Tabelle fest. `tests/
test_pii_registry_coverage.py` prueft, dass keine Tabelle mit
personenbezogenen Spalten hier fehlt, und faellt sonst rot — die Luecke
kann also nicht unbemerkt zurueckkommen.

Die Umgangsarten:

``LOESCHEN``      Zeile wird auf Verlangen geloescht und faellt unter die
                  Aufbewahrungsfrist des Betriebs.
``ANONYMISIEREN`` Zeile bleibt (Fremdschluessel, Geschaeftshistorie), die
                  personenbezogenen Felder werden geleert.
``NUR_AUSKUNFT``  Zeile wird gemeldet, aber nicht geloescht — handels- und
                  steuerrechtliche Aufbewahrung (GoBD, § 147 AO,
                  Art. 17 Abs. 3 lit. b DSGVO).
``KEIN_ENDKUNDE`` Enthaelt zwar Namen oder Adressen, aber keine Daten von
                  Endkunden des Betriebs (eigene Mitarbeiter, Betriebs-
                  stammdaten, Betreiber-Konten, Artikelbezeichnungen).
                  Gehoert nicht in die Endkunden-Auskunft.
"""
from __future__ import annotations

from dataclasses import dataclass, field

LOESCHEN = "loeschen"
ANONYMISIEREN = "anonymisieren"
NUR_AUSKUNFT = "nur_auskunft"
KEIN_ENDKUNDE = "kein_endkunde"

ALLE_UMGANGSARTEN = (LOESCHEN, ANONYMISIEREN, NUR_AUSKUNFT, KEIN_ENDKUNDE)


@dataclass(frozen=True)
class PiiTabelle:
    """Eine Tabelle und wie mit ihrem Personenbezug umzugehen ist."""

    tabelle: str
    umgang: str
    begruendung: str
    #: Spalten mit Personenbezug (fuer Auskunft und Anonymisierung).
    pii_spalten: tuple[str, ...] = ()
    #: Spalten, ueber die ein Betroffener gefunden wird.
    match_spalten: tuple[str, ...] = ()
    #: Wird in scripts/dsar.py behandelt?
    in_dsar: bool = False
    #: Faellt unter die automatische Loeschfrist (scripts/cleanup_pii.py)?
    in_cleanup: bool = False
    hinweise: str = ""


_EINTRAEGE: list[PiiTabelle] = [
    # ---- Endkundendaten, loeschbar -------------------------------------
    PiiTabelle(
        tabelle="email_conversations", umgang=LOESCHEN,
        begruendung="Mailverkehr mit dem Endkunden.",
        pii_spalten=("kunde_email", "kunde_name"),
        match_spalten=("kunde_email",),
        in_dsar=True, in_cleanup=True,
    ),
    PiiTabelle(
        tabelle="anfrage_tokens", umgang=LOESCHEN,
        begruendung="Anfrage-Formular des Endkunden; Antworten haengen "
                    "per FK CASCADE dran.",
        pii_spalten=("kunde_email", "kunde_name", "kunde_telefon"),
        match_spalten=("kunde_email", "kunde_telefon"),
        in_dsar=True, in_cleanup=True,
    ),
    PiiTabelle(
        tabelle="kundengespraeche", umgang=LOESCHEN,
        begruendung="Vor-Ort-Gespraech mit Diktat und Notizen.",
        pii_spalten=("kunde_name", "raw_transcript", "notizen_lang"),
        match_spalten=("kunde_name",),
        in_dsar=True, in_cleanup=True,
        hinweise="Nur Name-Match moeglich — deshalb in dsar.py hinter "
                 "--name-match.",
    ),
    PiiTabelle(
        tabelle="tenant_kunde_drive", umgang=LOESCHEN,
        begruendung="Zuordnung Endkunde → Drive-Ordner mit seinen Dateien.",
        pii_spalten=("kunde_name", "kunde_email", "kunde_telefon", "kunde_key"),
        match_spalten=("kunde_email", "kunde_telefon"),
        in_dsar=True,
        hinweise="dsar.py loescht mit --with-drive auch den Ordner selbst.",
    ),
    PiiTabelle(
        tabelle="visualisierungen", umgang=LOESCHEN,
        begruendung="Vorher-/Nachher-Fotos des Endkunden.",
        pii_spalten=("kunde_email", "kunde_name"),
        match_spalten=("kunde_email", "kunde_name"),
        in_dsar=True, in_cleanup=True,
    ),
    PiiTabelle(
        tabelle="rueckrufe", umgang=LOESCHEN,
        begruendung="Rueckrufbitte aus dem Telefonat: Name, Telefon und "
                    "das Anliegen im Freitext.",
        pii_spalten=("kunde_name", "kunde_telefon", "kunde_email", "anliegen"),
        match_spalten=("kunde_email", "kunde_telefon"),
        in_dsar=True, in_cleanup=True,
        hinweise="Fehlte bis 2026-08-23 in beiden Skripten — erledigte "
                 "Rueckrufe lagen unbegrenzt herum.",
    ),
    PiiTabelle(
        tabelle="kunde_external_ref", umgang=LOESCHEN,
        begruendung="Verweis des Endkunden in ein Fremdsystem; ohne den "
                    "Kunden wertlos.",
        pii_spalten=(),
        in_dsar=True,
        hinweise="Haengt per FK am Kunden und wird mit ihm behandelt.",
    ),

    # ---- Endkundendaten, nur anonymisierbar -----------------------------
    PiiTabelle(
        tabelle="kunden", umgang=ANONYMISIEREN,
        begruendung="Kundenakte. Loeschen wuerde Rechnungen und Auftraege "
                    "ihres Bezugs berauben, deshalb bleiben Zeile und ID "
                    "stehen und die Felder werden geleert.",
        pii_spalten=("name", "email", "telefon", "adresse"),
        match_spalten=("email", "telefon", "name"),
        in_dsar=True,
        hinweise="`anonymized_at` haelt fest, dass es passiert ist, und "
                 "macht den Vorgang wiederholbar.",
    ),
    PiiTabelle(
        tabelle="wissensluecken", umgang=ANONYMISIEREN,
        begruendung="Offene Kundenfrage. Die Frage selbst ist Betriebs"
                    "wissen und soll bleiben, der Fragesteller nicht.",
        pii_spalten=("kunde",),
        match_spalten=("kunde",),
        in_dsar=True, in_cleanup=True,
    ),

    # ---- Aufbewahrungspflichtig: melden, nicht loeschen ------------------
    PiiTabelle(
        tabelle="rechnungen", umgang=NUR_AUSKUNFT,
        begruendung="Rechnung: 10 Jahre Aufbewahrung (§ 147 AO, GoBD), "
                    "Art. 17 Abs. 3 lit. b DSGVO.",
        pii_spalten=("kunde_name", "kunde_strasse", "kunde_plz", "kunde_ort",
                     "kunde_email", "transcript"),
        match_spalten=("kunde_email", "kunde_name"),
        in_dsar=True,
    ),
    PiiTabelle(
        tabelle="angebote", umgang=NUR_AUSKUNFT,
        begruendung="Angebot als Teil der Geschaeftskorrespondenz "
                    "(6 Jahre, § 257 HGB).",
        pii_spalten=("kunde_name", "kunde_strasse", "kunde_plz", "kunde_ort",
                     "kunde_email", "mail_sent_to"),
        match_spalten=("kunde_email", "kunde_name"),
        in_dsar=True,
    ),
    PiiTabelle(
        tabelle="auftrag_stunden", umgang=NUR_AUSKUNFT,
        begruendung="Leistungsnachweis zum Auftrag; die Notiz kann den "
                    "Kunden erwaehnen.",
        pii_spalten=("notiz", "employee_name"),
        in_dsar=False,
        hinweise="Haengt am Auftrag, nicht direkt am Kunden — wird ueber "
                 "den Auftrag mitgemeldet.",
    ),

    # ---- Fristen ohne Endkundenbezug im engeren Sinn ---------------------
    PiiTabelle(
        tabelle="failed_mail_queue", umgang=LOESCHEN,
        begruendung="Nicht zugestellte Mails samt Empfaengeradresse.",
        pii_spalten=("recipient_email",),
        in_cleanup=True,
        hinweise="Reine Fristentabelle: der Empfaenger steht schon in der "
                 "Konversation, deshalb kein eigener Auskunftspfad.",
    ),
    PiiTabelle(
        tabelle="website_visits", umgang=LOESCHEN,
        begruendung="Besuchsereignisse der Website. Enthalten keine IP und "
                    "keinen Namen, nur eine Tageskennung — der Schluessel "
                    "dafuer wird nach zwei Tagen geloescht.",
        pii_spalten=("besucher_hash",),
        in_cleanup=True,
        hinweise="Bewusst NICHT in der Auskunft: eine Zuordnung zu einer "
                 "Person ist technisch unmoeglich, und genau das ist das "
                 "Ziel der Bauweise. Rohdaten fallen nach 14 Tagen weg, "
                 "danach bleiben nur anonyme Tagessummen.",
    ),
    PiiTabelle(
        tabelle="geocode_cache", umgang=LOESCHEN,
        begruendung="Zwischenspeicher fuer Kundenadressen, jederzeit neu "
                    "berechenbar.",
        pii_spalten=("address_normalized", "address_key"),
        in_cleanup=True,
        hinweise="Reine Fristentabelle ohne Kundenbezug in der Zeile — die "
                 "Adresse steht ohne Namen da und wird nachberechnet.",
    ),

    # ---- Keine Endkundendaten -------------------------------------------
    *[
        PiiTabelle(tabelle=t, umgang=KEIN_ENDKUNDE, begruendung=grund)
        for t, grund in [
            ("tenants", "Stammdaten des Betriebs selbst (Vertragspartner)."),
            ("employees", "Mitarbeiter des Betriebs, nicht dessen Kunden."),
            ("oauth_tokens", "Postfach-/Kalenderkonto des Betriebs."),
            ("app_sessions", "Anmeldesitzung eines Mitarbeiters."),
            ("app_login_tokens", "Anmelde-Link eines Mitarbeiters."),
            ("admin_users", "Konto des Plattformbetreibers."),
            ("admin_sessions", "Sitzung des Plattformbetreibers."),
            ("admin_login_attempts", "Anmeldeversuche am Betreiber-Backend."),
            ("admin_audit_log", "Protokoll der Betreiber-Aktionen."),
            ("tool_configs", "Konfiguration, kein Personenbezug."),
            ("website_tage", "Anonyme Tagessummen der Website — keine "
             "Besucherkennung mehr enthalten."),
            ("website_salt", "Taeglicher Zufallswert der Besucherzaehlung; "
             "wird nach zwei Tagen geloescht."),
            ("cron_heartbeats", "Lebenszeichen der Hintergrundjobs — `cron_name` ist ein Jobname, kein Personenname."),
            ("tenant_leistungen", "Leistungskatalog des Betriebs."),
            ("tenant_material", "Materialkatalog samt Lieferant (Firma)."),
            ("tenant_kalkulationen", "Kalkulationsvorlagen des Betriebs."),
            ("material_bestellung", "Bestellvorgang beim Lieferanten."),
            ("angebot_positionen", "Artikelbezeichnungen."),
            ("rechnung_positionen", "Artikelbezeichnungen."),
        ]
    ],
]

REGISTRY: dict[str, PiiTabelle] = {e.tabelle: e for e in _EINTRAEGE}


def eintrag(tabelle: str) -> PiiTabelle | None:
    return REGISTRY.get(tabelle)


def tabellen_mit_umgang(*umgang: str) -> list[PiiTabelle]:
    return [e for e in _EINTRAEGE if e.umgang in umgang]


def endkunden_tabellen() -> list[PiiTabelle]:
    """Alles, was in eine Endkunden-Auskunft gehoert."""
    return tabellen_mit_umgang(LOESCHEN, ANONYMISIEREN, NUR_AUSKUNFT)
