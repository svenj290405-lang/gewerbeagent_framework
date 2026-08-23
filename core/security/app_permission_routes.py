"""Welches Recht braucht welcher Endpunkt?

Eine zentrale Tabelle statt 115 einzelner Dekoratoren. Der Grund ist
nicht Bequemlichkeit, sondern Sicherheit: bei 115 Stellen vergisst man
eine, und eine vergessene Stelle ist ein stilles Loch, das niemandem
auffaellt. Hier faellt sie sofort auf — ``enforce_app_permission``
antwortet fuer jede Route, die NICHT in dieser Tabelle steht, mit 403,
und ``tests/test_permission_coverage.py`` macht daraus einen roten Test.

Schluessel ist der **Name der Endpunkt-Funktion**, nicht der Pfad:
Pfad-Templates aendern sich (``/team/{slug}`` vs. ``/team/{employee_slug}``),
Funktionsnamen bleiben, und Starlette setzt ``scope["endpoint"]`` bei
jedem Request zuverlaessig.

``OFFEN`` heisst: jeder eingeloggte Mitarbeiter darf. Das ist eine
bewusste Entscheidung pro Zeile, kein Default — deshalb steht es
explizit da und nicht als fehlender Eintrag.
"""
from __future__ import annotations

# Sentinel: kein besonderes Recht noetig, aber Login erforderlich.
OFFEN = "*offen*"


ROUTE_RECHTE: dict[str, str] = {
    # =================================================================
    # Geld — der eigentliche Gewinn dieser Umstellung.
    # Diese Endpunkte standen bisher JEDEM Mitarbeiter offen: Umsaetze,
    # offene Posten, alle Kundenbetraege.
    # =================================================================
    "api_buchhaltung": "buchhaltung.sehen",
    "api_buchhaltung_ausgaben": "buchhaltung.sehen",
    "api_angebote": "buchhaltung.sehen",
    "api_rechnungen": "buchhaltung.sehen",
    "api_belege_list": "buchhaltung.sehen",

    "api_angebot_anlegen": "buchhaltung.fuehren",
    "api_angebot_senden": "buchhaltung.fuehren",
    "api_angebot_extrahieren": "buchhaltung.fuehren",
    "api_rechnung_anlegen": "buchhaltung.fuehren",
    "api_rechnung_senden": "buchhaltung.fuehren",
    "api_rechnung_extrahieren": "buchhaltung.fuehren",
    "api_rechnung_vorbereiten": "buchhaltung.fuehren",
    "api_q_rechnung_senden": "buchhaltung.fuehren",
    "api_rechnungen_pruefen": "buchhaltung.fuehren",
    "api_erinnerung_entwurf": "buchhaltung.fuehren",
    "api_erinnerung_senden": "buchhaltung.fuehren",
    # Belege: Hochladen darf jeder (der Monteur fotografiert den
    # Tankbeleg), Kontieren ist eine Buchhaltungs-Entscheidung.
    "api_beleg_upload": OFFEN,
    "api_beleg_vorschlag": "buchhaltung.fuehren",
    "api_beleg_kontieren": "buchhaltung.fuehren",

    # =================================================================
    # Auftraege
    # Lesen ist OFFEN, aber zeilenweise gefiltert: ohne
    # `auftraege.alle_sehen` sieht jemand nur die eigenen. Das erledigt
    # der Scope-Filter im Service, nicht dieses Gate.
    # =================================================================
    "api_auftraege": OFFEN,
    "api_auftraege_abgeschlossen": OFFEN,
    "api_auftraege_historie": OFFEN,
    "api_auftrag_detail": OFFEN,
    "api_auftragsprozess": OFFEN,
    # Arbeit am eigenen Auftrag — Fortschritt, Schritte, Stunden.
    "api_auftrag_fortschritt": OFFEN,
    "api_auftrag_schritt": OFFEN,
    "api_auftrag_stunden_buchen": OFFEN,
    "api_auftrag_stunden_loeschen": OFFEN,

    "api_auftrag_neu": "auftraege.fuehren",
    "api_auftrag_status": "auftraege.fuehren",
    "api_auftrag_zuweisen": "auftraege.fuehren",
    "api_auftragsprozess_speichern": "auftraege.fuehren",

    # =================================================================
    # Kunden, Gespraeche, Anfragen
    # =================================================================
    "api_kunden": OFFEN,
    "api_kunde_profil": OFFEN,
    "api_kunden_merge": "kunden.pflegen",
    "api_gespraech_kunde_zuordnen": "kunden.pflegen",

    "api_gespraeche": OFFEN,
    "api_gespraech_neu": OFFEN,
    "api_gespraeche_geplant": OFFEN,
    "api_gespraech_detail": OFFEN,
    "api_gespraech_abschliessen": OFFEN,
    "api_gespraech_verwerfen": OFFEN,
    "api_gespraech_notiz": OFFEN,
    "api_gespraech_foto": OFFEN,
    "api_gespraech_diktat": OFFEN,
    "api_gespraech_mail": OFFEN,
    "api_aufnahmen": OFFEN,
    "api_aufnahme_detail": OFFEN,
    "api_aufnahme_diktat": OFFEN,
    "api_beratung_entscheidung": OFFEN,

    "api_anfragen_list": OFFEN,
    "api_anfrage_detail": OFFEN,
    "api_anfrage_reply": "anfragen.bearbeiten",

    # =================================================================
    # Termine, Rueckrufe, Material, Archiv, Wissen
    # =================================================================
    "api_termine": OFFEN,
    "api_termin_anlegen": OFFEN,
    "api_termin_storno": OFFEN,
    "api_freie_slots": OFFEN,

    "api_rueckrufe": OFFEN,
    "api_rueckruf_anlegen": OFFEN,
    "api_rueckruf_erledigt": OFFEN,

    "api_material_list": OFFEN,
    "api_material_bestellungen": OFFEN,
    "api_material_bestellen": OFFEN,
    "api_material_anlegen": "material.verwalten",
    "api_material_toggle": "material.verwalten",

    "api_archiv_dateien": OFFEN,
    "api_archiv_datei_proxy": OFFEN,
    "api_archiv_upload": OFFEN,
    "api_archiv_notiz": OFFEN,

    # Wissensbasis. Lesen ist OFFEN — die App ist das Werkzeug des
    # Betriebs, "intern" grenzt gegen den KUNDEN ab, nicht gegen
    # Kollegen. Pflegen bleibt beim Recht.
    "api_wissen": OFFEN,
    "api_wissen_add": "wissen.pflegen",
    "api_wissen_update": "wissen.pflegen",
    "api_wissen_bestaetigen": "wissen.pflegen",
    "api_wissen_delete": "wissen.pflegen",
    "api_wissen_pruefung": OFFEN,
    # Der Import holt eine vom Nutzer genannte URL vom Server. Das ist
    # eine ausgehende Verbindung im Namen des Betriebs — gehoert hinter
    # dasselbe Recht wie das Pflegen selbst.
    "api_wissen_import_website": "wissen.pflegen",
    # Der Datenexport zieht ALLE Daten des Betriebs in ein ZIP —
    # das gehoert an dasselbe Recht wie die Stammdaten selbst.
    "api_datenexport": "einstellungen.verwalten",
    "api_wissen_import_datei": "wissen.pflegen",
    # Einrichtungs-Interview: die Fragen darf jeder sehen (auch als
    # Durchsicht "was steht zu Preisen drin?"), Antworten speichern heisst
    # Wissens-Eintraege anlegen.
    "api_wissen_interview": OFFEN,
    "api_wissen_interview_speichern": "wissen.pflegen",
    "api_wissen_import_uebernehmen": "wissen.pflegen",

    # Wissensluecken: die unbeantworteten Kundenfragen. Sehen darf sie
    # jeder (auch ein Monteur weiss oft die Antwort), beantworten heisst
    # aber einen Wissens-Eintrag anlegen — also dasselbe Recht.
    "api_wissensluecken": OFFEN,
    "api_wissensluecke_beantworten": "wissen.pflegen",
    "api_wissensluecke_verwerfen": "wissen.pflegen",

    # Ueberschlags-Formeln. Rechnen darf jeder, der beim Kunden steht;
    # die Ansaetze aendern ist eine Preisentscheidung.
    "api_kalkulationen": OFFEN,
    "api_kalkulation_rechnen": OFFEN,
    "api_kalkulation_add": "wissen.pflegen",
    "api_kalkulation_delete": "wissen.pflegen",

    "api_visualisierungen": OFFEN,
    "api_visualisierung_erstellen": OFFEN,
    "api_visualisierung_bild": OFFEN,

    # =================================================================
    # Team
    # `api_team` liefert auch die App-Nutzung der Kollegen (Logins,
    # Diktate, Q-Befehle). Das ist Mitarbeiter-Monitoring und gehoert
    # hinter ein Recht — der Endpunkt filtert die Kennzahlen zusaetzlich
    # selbst heraus, wenn `team.fuehren` fehlt.
    # =================================================================
    "api_team": "team.sehen",
    "api_team_anlegen": "team.fuehren",
    "api_team_set_active": "team.fuehren",
    "api_team_set_profile": "team.fuehren",
    "api_team_abwesenheit": "team.fuehren",
    "api_team_zurueck": "team.fuehren",
    "api_team_rechte": "team.rechte",
    "api_team_set_rolle": "team.rechte",
    "api_team_set_recht": "team.rechte",

    # =================================================================
    # Einstellungen, Verbindungen, Formulare, Branding
    # Lesen bleibt offen (die Screens zeigen Stammdaten read-only),
    # Schreiben ist Inhaber-Sache.
    # =================================================================
    "api_einstellungen_get": OFFEN,
    "api_einstellungen_set": "einstellungen.verwalten",
    "api_automatisierung_get": OFFEN,
    "api_automatisierung_set": "einstellungen.verwalten",
    "api_branding_logo": OFFEN,
    "api_branding_logo_upload": "einstellungen.verwalten",
    "api_branding_logo_delete": "einstellungen.verwalten",

    "api_verbindungen_get": "einstellungen.verwalten",
    "api_verbindungen_trennen": "einstellungen.verwalten",
    "api_verbindung_test": "einstellungen.verwalten",
    "api_lexware_verbinden": "einstellungen.verwalten",
    # OAuth-Start bleibt vorerst Betriebs-Sache. Die Selbstverbindung
    # des eigenen Kalenders bekommt eigene Endpunkte (spaetere Stufe).
    "api_oauth_start": "einstellungen.verwalten",

    # Der EIGENE Kalender ist keine Betriebs-Einstellung: den darf
    # jeder Mitarbeiter selbst anschliessen und wieder trennen.
    # Der Slug kommt dabei aus der Session, nie vom Client.
    "api_mein_kalender": OFFEN,
    "api_mein_kalender_verbinden": OFFEN,
    "api_mein_kalender_trennen": OFFEN,

    "api_formular_get": "einstellungen.verwalten",
    "api_formular_save": "einstellungen.verwalten",
    "api_formular_reset": "einstellungen.verwalten",
    "api_formular_vorschau": "einstellungen.verwalten",
    "api_formular_q": "einstellungen.verwalten",
    # Einen Formular-Link erzeugen und dem Kunden geben darf jeder —
    # das ist Alltagsarbeit, keine Konfiguration.
    "api_formular_link_generieren": OFFEN,

    # =================================================================
    # Uebersicht, Assistent, Shell — fuer jeden
    # =================================================================
    "api_dashboard": OFFEN,
    "api_aktuelles": OFFEN,
    "api_briefing": OFFEN,
    "api_diagnose": OFFEN,

    "api_assistent": OFFEN,
    "api_assistent_ausfuehren": OFFEN,
    "api_assistent_mit_bild": OFFEN,
    "api_assistent_transkript": OFFEN,
    "api_objekt_frage": OFFEN,
    "api_objekt_merken": OFFEN,

    "app_api_me": OFFEN,
    "app_onboarding_complete": OFFEN,
    "app_push_subscribe": OFFEN,
    "app_push_unsubscribe": OFFEN,
    "app_shell_root": OFFEN,
    "app_shell_beta": OFFEN,
    "app_logout": OFFEN,
}


# Endpunkte ohne Login — bewusst ausserhalb des Rechtesystems.
# Jede Zeile ist eine Entscheidung, keine Auslassung.
OEFFENTLICHE_ENDPUNKTE: frozenset[str] = frozenset({
    "app_login_page",       # Login-Formular
    "app_login_request",    # Magic-Link anfordern
    "app_login_password",   # Passwort-Login
    "app_login_consume",    # Magic-Link einloesen
    "app_activate_page",    # Mitarbeiter-Aktivierung (Token = Ausweis)
    "app_activate_info",
    "app_activate_set",
    "app_manifest",         # PWA-Manifest
    "app_service_worker",   # Service Worker
})


def recht_fuer_endpunkt(name: str) -> str | None:
    """Recht fuer einen Endpunkt-Funktionsnamen.

    ``OFFEN`` -> jeder eingeloggte Nutzer.
    ``None``  -> Endpunkt ist NICHT eingetragen; der Aufrufer muss
    fail-closed reagieren.
    """
    return ROUTE_RECHTE.get(name)
