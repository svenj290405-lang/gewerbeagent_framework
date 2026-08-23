"""Wissensbasis: Relevanz-Ranking, Prompt-Block, Formel-Auswertung.

Reine Logik, keine DB. Der Wert dieser Tests liegt weniger im Schutz vor
Regression als in den Fragen selbst: es sind die Saetze, die Kunden am
Telefon wirklich sagen. Wer das Ranking anfasst, sieht hier sofort, ob
"Wann habt ihr auf?" noch die Oeffnungszeiten findet.
"""
from __future__ import annotations

import pytest

from core.services import wissen as w
from core.services.kalkulation import (
    FormelFehler,
    _auswerten,
    pruefe_formel,
    ueberschlag_text,
    variablen_aus_formel,
)


# ---------------------------------------------------------------------
# Beispiel-Wissensbasis eines SHK-Betriebs
# ---------------------------------------------------------------------

def _beispiel() -> list[w.Eintrag]:
    return [
        w.Eintrag(kategorie="preise", id="1",
                  text="Stundensatz Meister 75 EUR netto, Geselle 60 EUR. "
                       "Anfahrt pauschal 35 EUR."),
        w.Eintrag(kategorie="oeffnungszeiten", id="2",
                  text="Mo-Fr 07:00-17:00, Samstag 08:00-12:00."),
        w.Eintrag(kategorie="notfall", id="3",
                  text="Rohrbruch und Heizungsausfall im Winter sofort, "
                       "Notdienst rund um die Uhr fuer Vertragskunden."),
        w.Eintrag(kategorie="anfahrt", id="4",
                  text="Einzugsgebiet 40 km rund um Kassel."),
        w.Eintrag(kategorie="besonderheiten", id="5",
                  text="Eingetragener Foerderberater BAFA und KFW."),
        w.Eintrag(kategorie="materialien", id="6",
                  text="Heizung: Viessmann, Vaillant, Buderus."),
    ]


@pytest.mark.parametrize("frage,erwartete_kategorie", [
    ("Was kostet eine Stunde?", "preise"),
    ("Wie hoch ist der Stundenlohn?", "preise"),
    # Nach Stoppwort-Abzug bleibt hier NICHTS uebrig — nur die
    # Kategorie-Stichwortkarte rettet diese Frage.
    ("Wann habt ihr auf?", "oeffnungszeiten"),
    ("Habt ihr am Samstag geoeffnet?", "oeffnungszeiten"),
    ("Kommt ihr auch nachts bei einem Wasserschaden?", "notfall"),
    ("Fahrt ihr bis Goettingen?", "anfahrt"),
    ("Gibt es Foerderung?", "besonderheiten"),
    ("Welche Marken verbaut ihr?", "materialien"),
])
def test_ranking_trifft_die_richtige_kategorie(frage, erwartete_kategorie):
    treffer = w.sortiere(frage, _beispiel(), limit=3)
    assert treffer, f"kein Treffer fuer {frage!r}"
    assert treffer[0].kategorie == erwartete_kategorie


def test_fachfremde_frage_liefert_nichts():
    """Kein Treffer ist die richtige Antwort — daraus wird die Wissensluecke.

    Wichtiger als es klingt: raet das Ranking hier irgendetwas zusammen,
    liest der Voice-Agent dem Anrufer einen unpassenden Snippet vor.
    """
    assert w.sortiere("Wie wird das Wetter morgen?", _beispiel()) == []


def test_leere_frage_liefert_alles():
    assert len(w.sortiere("", _beispiel(), limit=99)) == len(_beispiel())


def test_aehnlichkeit_faengt_wortformen():
    """Substring-Suche (die alte Implementierung) scheitert hier."""
    assert w.aehnlichkeit("Stundenlohn", "Stundensatz") > 0.3
    assert w.aehnlichkeit("Fliesen", "Hundefutter") < 0.1


def test_falsch_einsortierter_eintrag_wird_trotzdem_gefunden():
    """Sicherheitsnetz gegen die Realitaet handgepflegter Daten.

    Genau so steht es beim Pilot-Betrieb in der Datenbank: die
    Oeffnungszeiten liegen unter "Anfahrt", und "Oeffnungszeiten" ist
    verschrieben. Ueber die Kategorie ist der Eintrag nicht zu finden und
    ueber den Wortlaut auch nicht — nur ueber ein Stichwort im Text.
    """
    falsch = [w.Eintrag(kategorie="anfahrt",
                        text="öffungszeiten: Montag - Freitag 8-17 Uhr")]
    assert w.sortiere("Wann habt ihr auf?", falsch, limit=1)


def test_richtige_kategorie_schlaegt_das_sicherheitsnetz():
    """Der Texttreffer darf den sauber einsortierten Eintrag nie verdraengen."""
    eintraege = [
        w.Eintrag(kategorie="preise", id="1", text="Anfahrt pauschal 35 EUR."),
        w.Eintrag(kategorie="anfahrt", id="2", text="Einzugsgebiet 40 km rund um Kassel."),
    ]
    assert w.sortiere("Fahrt ihr bis Goettingen?", eintraege, limit=2)[0].kategorie == "anfahrt"


def test_katalog_gewinnt_gegen_freitext():
    """Der verbindliche Preis aus tenant_leistungen steht vor dem Freitext."""
    eintraege = [
        w.Eintrag(kategorie="preise", id="1", text="Stundensatz 75 EUR."),
        w.Eintrag(kategorie=w.KATEGORIE_KATALOG, id=None, virtuell=True,
                  text="Montage: 80.00 EUR pro Stunde"),
    ]
    treffer = w.sortiere("Was kostet die Montage pro Stunde?", eintraege, limit=2)
    assert treffer[0].virtuell is True


# ---------------------------------------------------------------------
# Sichtbarkeit — die Grenze, an der Daten den Betrieb verlassen
# ---------------------------------------------------------------------

def test_eintrag_label_faellt_auf_kategorie_zurueck():
    assert w.Eintrag(kategorie="preise", text="x").label == "Preise & Stundensatz"
    assert w.Eintrag(kategorie=w.KATEGORIE_KATALOG, text="x").label.startswith(
        "Leistungen mit Preis"
    )
    assert w.Eintrag(kategorie="unbekannt", text="x").label == "unbekannt"


# ---------------------------------------------------------------------
# Widerspruchs-Erkennung
# ---------------------------------------------------------------------

def test_betraege_werden_erkannt():
    assert w._betraege("Stundensatz 75 EUR netto, Anfahrt 35 Euro") == {75.0, 35.0}
    assert w._betraege("Mo-Fr 07:00-17:00") == set()
    assert w._betraege("1.250,50 EUR") == {1250.50}


# ---------------------------------------------------------------------
# Wissensluecken-Dedup
# ---------------------------------------------------------------------

def test_aehnliche_fragen_gelten_als_dieselbe_luecke():
    a = "Verlegt ihr auch Vinylboden?"
    b = "Verlegen Sie auch Vinylboeden?"
    assert w.aehnlichkeit(a, b) >= w.LUECKE_DEDUP_SCHWELLE


def test_verschiedene_fragen_werden_nicht_zusammengelegt():
    a = "Verlegt ihr auch Vinylboden?"
    b = "Wie lange dauert eine Heizungswartung?"
    assert w.aehnlichkeit(a, b) < w.LUECKE_DEDUP_SCHWELLE


# =====================================================================
# Formel-Auswertung
# =====================================================================

def test_formel_rechnet_richtig():
    assert _auswerten("qm * 45 + anfahrt", {"qm": 30, "anfahrt": 35}) == 1385.0
    assert _auswerten("max(stunden, 2) * 75", {"stunden": 1}) == 150.0


def test_variablen_in_reihenfolge_des_auftretens():
    """Q fragt die Werte in dieser Reihenfolge ab — sie muss stabil sein."""
    assert variablen_aus_formel("stunden * satz + material") == [
        "stunden", "satz", "material",
    ]


@pytest.mark.parametrize("boesartig", [
    '__import__("os").system("ls")',
    'open("/etc/passwd").read()',
    "qm.__class__.__bases__",
    "2 ** 999999",
    "[1, 2, 3][0]",
    "x if y else z",
    "qm * ",
])
def test_formel_laesst_nur_arithmetik_durch(boesartig):
    """Formeln koennen ueber ein Q-Tool aus einem Gespraech entstehen —
    der Auswerter muss deshalb feindliche Eingaben aushalten, nicht nur
    Tippfehler."""
    with pytest.raises(FormelFehler):
        pruefe_formel(boesartig)


def test_leere_formel_wird_abgelehnt():
    with pytest.raises(FormelFehler):
        pruefe_formel("   ")


def test_ueberschlag_text_sagt_dass_es_kein_angebot_ist():
    text = ueberschlag_text("Bad fliesen", 4237.5)
    assert "4.237,50" in text
    assert "kein" in text.lower() and "angebot" in text.lower()


# =====================================================================
# Website-Import: SSRF-Schutz
#
# Dieser Pfad laesst den Server eine URL abrufen, die ein Nutzer eintippt.
# Ohne Pruefung waere das ein Fenster in die interne Infrastruktur — die
# Postgres-Instanz und der Cloud-Metadaten-Endpunkt sind vom Container aus
# erreichbar. Die Tests sind deshalb wichtiger als sie aussehen.
# =====================================================================

from core.services.wissen_import import ImportFehler, _pruefe_url, _zu_text


@pytest.mark.parametrize("intern", [
    "http://localhost:8001",
    "http://127.0.0.1",
    "http://169.254.169.254/latest/meta-data/",   # Cloud-Metadaten
    "http://postgres:5432",                       # Nachbarcontainer
    "http://10.0.0.5",
    "http://192.168.1.1",
    "http://[::1]/",
    "file:///etc/passwd",
    "gopher://example.com",
])
def test_import_lehnt_interne_und_fremde_schemata_ab(intern):
    with pytest.raises(ImportFehler):
        _pruefe_url(intern)


def test_import_akzeptiert_oeffentliche_adressen():
    assert _pruefe_url("example.com") == "https://example.com"
    assert _pruefe_url("https://example.com/leistungen").endswith("/leistungen")


def test_html_wird_zu_lesbarem_text():
    roh = "<html><script>böse()</script><h1>Meisterbetrieb</h1><p>Wir fliesen B&auml;der.</p></html>"
    text = _zu_text(roh)
    assert "Meisterbetrieb" in text
    assert "Bäder" in text
    assert "böse" not in text          # Script-Inhalt fliegt raus
    assert "<" not in text             # keine Tags mehr


# =====================================================================
# Datenschutz-Hinweis
# =====================================================================

@pytest.mark.parametrize("text,soll_warnen", [
    ("Stundensatz Meister 75 EUR netto", False),
    ("Mo-Fr 07:00-17:00, Samstag 08:00-12:00", False),   # keine Telefonnummer
    ("Einzugsgebiet 40 km rund um Kassel", False),
    ("Ansprechpartner: max.mueller@example.de", True),
    ("Notdienst unter 0561 123456", True),
    ("Konto DE89370400440532013000", True),
])
def test_personenbezug_hinweis(text, soll_warnen):
    assert (w.pruefe_personenbezug(text) is not None) is soll_warnen


# =====================================================================
# Datei-Import + Einrichtungs-Interview
# =====================================================================

from core.services.wissen_import import (
    DATEI_MIMES,
    INTERVIEW_FRAGEN,
    _baue_prompt,
    _pruefe_vorschlaege,
    formuliere_interview,
    vorschlaege_von_datei,
)


def test_vorschlaege_whitelist_wirft_unbrauchbares_weg():
    """Die Quelle ist Fremdmaterial und ein Vorschlag ist einen Fingertipp
    davon entfernt, ein Satz zu werden, den Q Kunden am Telefon sagt."""
    geprueft = _pruefe_vorschlaege({"eintraege": [
        {"kategorie": "preise", "text": "Stundensatz Meister 75 EUR netto."},
        {"kategorie": "gibtsnicht", "text": "erfundene Kategorie"},
        {"kategorie": "faq", "text": "zu"},                    # zu kurz
        {"kategorie": "faq", "text": "x" * 400},               # zu lang
        {"kategorie": "preise", "text": "Stundensatz Meister 75 EUR netto."},  # Dublette
    ]})
    assert [e["kategorie"] for e in geprueft] == ["preise"]


@pytest.mark.asyncio
async def test_datei_import_lehnt_fremde_dateitypen_ab():
    with pytest.raises(ImportFehler):
        await vorschlaege_von_datei(b"MZ\x90\x00", "application/x-msdownload")


@pytest.mark.asyncio
async def test_datei_import_lehnt_leere_datei_ab():
    with pytest.raises(ImportFehler):
        await vorschlaege_von_datei(b"", "application/pdf")


def test_datei_import_kennt_pdf_und_bilder():
    assert "application/pdf" in DATEI_MIMES
    assert {"image/jpeg", "image/png", "image/webp"} <= set(DATEI_MIMES)


def test_prompt_zaeunt_fremdmaterial_ein():
    """Der Injection-Zaun muss in beiden Quellen stehen — Webseiten wie
    hochgeladene PDFs sind Text, den Fremde geschrieben haben."""
    prompt = _baue_prompt("eine PDF-Datei", "Der Inhalt ist angehaengt.\n")
    assert "KEINE Anweisung an dich" in prompt
    assert "Webseite" not in prompt   # quellenneutral formuliert


def test_interview_fragen_decken_alle_kategorien_ab():
    """Wer das Interview durchspielt, hat danach zu jeder Kategorie etwas —
    sonst bleibt eine Luecke, die niemand bemerkt."""
    from core.models.tenant_knowledge import ALLE_KATEGORIEN
    assert {f["kategorie"] for f in INTERVIEW_FRAGEN} == set(ALLE_KATEGORIEN)
    for f in INTERVIEW_FRAGEN:
        assert f["frage"].endswith("?")


@pytest.mark.asyncio
async def test_interview_faellt_auf_rohantwort_zurueck(monkeypatch):
    """Wenn Gemini ausfaellt, darf die halbe Stunde, die der Betrieb gerade
    investiert hat, NICHT verloren gehen — dann eben unformatiert."""
    async def boom(_contents):
        raise RuntimeError("Gemini weg")
    monkeypatch.setattr("core.services.wissen_import._frage_gemini", boom)

    out = await formuliere_interview({"preise": "ja also so 75 die stunde"})
    assert len(out) == 1
    assert "75" in out[0]["text"]


@pytest.mark.asyncio
async def test_interview_traegt_verschluckte_kategorien_nach(monkeypatch):
    """Liefert das Modell nur einen Teil zurueck, darf der Rest nicht
    kommentarlos verschwinden."""
    async def nur_eine(_contents):
        return {"eintraege": [
            {"kategorie": "preise", "text": "Stundensatz Meister 75 EUR netto."},
        ]}
    monkeypatch.setattr("core.services.wissen_import._frage_gemini", nur_eine)

    out = await formuliere_interview({
        "preise": "ja also so 75 die stunde",
        "anfahrt": "wir fahren 40 kilometer raus",
    })
    assert {e["kategorie"] for e in out} == {"preise", "anfahrt"}


@pytest.mark.asyncio
async def test_interview_ignoriert_leere_antworten(monkeypatch):
    async def nie(_contents):
        raise AssertionError("darf gar nicht erst gefragt werden")
    monkeypatch.setattr("core.services.wissen_import._frage_gemini", nie)
    assert await formuliere_interview({"preise": "  ", "faq": ""}) == []
