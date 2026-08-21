"""Tests fuer die Gemini-Kommando-Zentrale (core/ai/command_center.py).

Reine Unit-Tests mit einem gefaketen genai-Client — keine echte DB, kein
Netz (Muster wie test_app_material_bestellung.py / test_app_diktat.py).

Gefaket wird ausschliesslich ``_get_genai_client``; die echten genai-Typen
(Tool/FunctionDeclaration/Part/Content) werden gebaut, aber der
``generate_content``-Call liefert skriptete Antworten zurueck. Die Tool-
``run``-Funktionen werden pro Test gepatcht, damit nichts an die DB geht.

Deckt:
- Tool-Gating: Feature- und Inhaber-Filter
- Read-Tool-Schleife: Tool laeuft, Ergebnis fliesst zurueck -> message
- Write-Tool: liefert confirm-Vorschlag, fuehrt NICHT aus
- execute_confirmed: fuehrt Write-Tool aus, gegated
- Defensive: unbekanntes/ungegatetes Tool wird abgewiesen
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from core.ai import command_center as cc


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------

def _part_fc(name, args):
    return SimpleNamespace(function_call=SimpleNamespace(name=name, args=args), text=None)


def _part_text(text):
    return SimpleNamespace(function_call=None, text=text)


def _resp(parts):
    content = SimpleNamespace(parts=parts)
    return SimpleNamespace(candidates=[SimpleNamespace(content=content)])


class _FakeModels:
    def __init__(self, scripted):
        self.scripted = scripted
        self.calls = 0
        self.seen_contents = []

    def generate_content(self, *, model, contents, config):
        self.seen_contents.append(contents)
        resp = self.scripted[self.calls]
        self.calls += 1
        return resp


class _FakeClient:
    def __init__(self, models):
        self.models = models


def _patch_gemini(monkeypatch, scripted):
    """Patcht _get_genai_client so, dass generate_content die skripteten
    Antworten liefert. Gibt das _FakeModels-Objekt zurueck (fuer Asserts)."""
    models = _FakeModels(scripted)
    import core.ai.gemini as gem
    monkeypatch.setattr(gem, "_get_genai_client", lambda location="x": _FakeClient(models))
    return models


def _patch_tool(monkeypatch, name, fake_run):
    """Patcht die run-Funktion eines Tools in der Registry."""
    spec = cc._spec_by_name(name)
    monkeypatch.setattr(spec, "run", fake_run)
    return spec


def _ctx(features=("kalender",), is_inhaber=True, permissions=None):
    """Ctx fuer die Tool-Gating-Tests.

    Seit der Einfuehrung der Rechte entscheidet nicht mehr is_default,
    welche Tools Q anbietet, sondern ``permissions``. Ohne explizite
    Angabe bekommt der Inhaber alle Rechte und ein Nicht-Inhaber die
    Monteur-Vorlage — damit bleiben die Tests so lesbar wie vorher.
    """
    from core.features.permissions import (
        ALLE_RECHTE, ROLLE_MONTEUR, rechte_fuer_rolle,
    )

    emp = SimpleNamespace(id=uuid.uuid4(), name="Sven Jantos", slug="sven",
                          is_default=is_inhaber)
    tenant = SimpleNamespace(id=uuid.uuid4(), slug="pilot", company_name="Jantos GmbH")
    if permissions is None:
        permissions = ALLE_RECHTE if is_inhaber else rechte_fuer_rolle(ROLLE_MONTEUR)
    return cc.Ctx(
        tenant=tenant, employee=emp, tid=tenant.id, features=set(features),
        permissions=frozenset(permissions),
    )


# --------------------------------------------------------------------------
# Gating
# --------------------------------------------------------------------------

def test_inhaber_gate_filters_abwesenheit():
    inhaber = _ctx(is_inhaber=True)
    monteur = _ctx(is_inhaber=False)
    names_inhaber = {s.name for s in cc._available_tools(inhaber)}
    names_monteur = {s.name for s in cc._available_tools(monteur)}
    assert "abwesenheit_melden" in names_inhaber
    assert "abwesenheit_melden" not in names_monteur


def test_feature_gate_filters_kalender_tools():
    ohne = _ctx(features=())
    names = {s.name for s in cc._available_tools(ohne)}
    assert "termin_anlegen" not in names
    assert "freie_termine_finden" not in names
    assert "termin_stornieren" not in names
    # Nicht-gegatete Tools bleiben verfuegbar:
    assert "rueckruf_anlegen" in names
    assert "kunde_suchen" in names


def test_feature_gate_filters_mail_and_lexware_tools():
    ohne = _ctx(features=())
    names = {s.name for s in cc._available_tools(ohne)}
    assert "offene_anfragen" not in names          # braucht mail_intake
    assert "auftrag_status" not in names            # braucht lexware (+Inhaber)
    assert "archiv_suchen" not in names             # braucht drive_archiv
    assert "rechnungen_pruefen" not in names        # braucht lexware
    assert "formulare_status" not in names          # braucht anfrage_formular
    assert "team_status" in names
    assert "wissen_suchen" in names
    assert "wissen_merken" in names
    assert "wissen_loeschen" in names               # ungegated
    assert "rueckruf_erledigt" in names


def test_drive_tools_gated_by_drive_archiv():
    # Ohne das Feature darf Q gar nicht erst anbieten, in Drive zu schreiben.
    ohne = _ctx(features=())
    names = {s.name for s in cc._available_tools(ohne)}
    assert "drive_ordner_anlegen" not in names
    assert "drive_notiz_anlegen" not in names

    # Mit drive_archiv sind beide da — auch fuer den Monteur: einen Kunden-
    # ordner anlegen oder eine Notiz ablegen ist Alltag auf der Baustelle,
    # kein Inhaber-Vorbehalt (anders als Angebot/Rechnung).
    monteur = _ctx(features=("drive_archiv",), is_inhaber=False)
    names2 = {s.name for s in cc._available_tools(monteur)}
    assert "drive_ordner_anlegen" in names2
    assert "drive_notiz_anlegen" in names2
    assert "archiv_suchen" in names2


def test_drive_tools_sind_write_tools():
    # Beide schreiben nach Drive -> muessen ueber den confirm-Pfad laufen und
    # duerfen nicht als Read-Tool stillschweigend ausgefuehrt werden.
    for name in ("drive_ordner_anlegen", "drive_notiz_anlegen"):
        spec = cc._spec_by_name(name)
        assert spec is not None, f"{name} fehlt in der Registry"
        assert spec.kind == "write"
        assert spec.feature == "drive_archiv"
        assert spec.summarize is not None, f"{name} braucht eine confirm-Zusammenfassung"


def test_archiv_and_lexware_tools_appear_with_features():
    mit = _ctx(features=("drive_archiv", "lexware", "anfrage_formular"))
    names = {s.name for s in cc._available_tools(mit)}
    assert "archiv_suchen" in names
    assert "rechnungen_pruefen" in names
    assert "formulare_status" in names


def test_inhaber_gate_filters_new_write_tools():
    monteur = _ctx(features=("kalender", "lexware", "mail_intake"), is_inhaber=False)
    names = {s.name for s in cc._available_tools(monteur)}
    assert "mitarbeiter_zurueck" not in names
    assert "material_anlegen" not in names
    assert "auftrag_status" not in names
    assert "rueckruf_erledigt" in names
    assert "anstehende_termine" in names


def test_beleg_fluss_tools_gating():
    # Inhaber mit lexware+mail sieht den ganzen Beleg-Fluss
    full = _ctx(features=("lexware", "mail_intake"), is_inhaber=True)
    names = {s.name for s in cc._available_tools(full)}
    for t in ("angebot_erstellen", "angebot_senden", "rechnung_erstellen",
              "rechnung_abrechnen", "anfrage_beantworten"):
        assert t in names
    # Ohne lexware: keine Angebot/Rechnung-Tools, aber anfrage_beantworten (mail_intake)
    ohne_lex = _ctx(features=("mail_intake",), is_inhaber=True)
    names2 = {s.name for s in cc._available_tools(ohne_lex)}
    assert "angebot_erstellen" not in names2
    assert "rechnung_abrechnen" not in names2
    assert "anfrage_beantworten" in names2
    # Monteur (kein Inhaber): weder Angebot/Rechnung noch Anfrage-Antwort.
    # Der HTTP-Endpunkt api_anfrage_reply verlangt `anfragen.bearbeiten`;
    # boete Q das Tool trotzdem an, waere der Chat der Weg um das Gate
    # herum — im Namen des Betriebs eine Kundenmail zu schreiben ist
    # genau die Sorte Aktion, die dieser Weg nicht aufmachen darf.
    monteur = _ctx(features=("lexware", "mail_intake"), is_inhaber=False)
    names3 = {s.name for s in cc._available_tools(monteur)}
    assert "angebot_erstellen" not in names3
    assert "rechnung_abrechnen" not in names3
    assert "anfrage_beantworten" not in names3
    # Mit dem Recht dagegen schon — Rolle egal, das Recht entscheidet.
    mit_recht = _ctx(features=("mail_intake",), is_inhaber=False,
                     permissions={"anfragen.bearbeiten"})
    assert "anfrage_beantworten" in {s.name for s in cc._available_tools(mit_recht)}


# Soll-Bestand der Registry. Bewusst eine Namensmenge statt einer Anzahl:
# ein Zaehler bricht bei jedem neuen Tool, ohne zu sagen welches fehlt, und
# haelt ein versehentlich geloeschtes Tool nicht auf, solange nur die Summe
# stimmt. Wer ein Tool ergaenzt, traegt es hier bewusst nach.
_ERWARTETE_TOOLS = {
    # read
    "freie_termine_finden", "kunde_suchen", "material_liste", "material_bestellungen",
    "offene_rueckrufe",
    "anzeige_oeffnen", "anstehende_termine", "team_status", "offene_anfragen",
    "wissen_suchen", "archiv_suchen", "archiv_dateien", "rechnungen_pruefen",
    "offene_posten", "formulare_status",
    # write
    "termin_anlegen", "termin_stornieren", "termin_verschieben", "rueckruf_anlegen",
    "rueckruf_erledigt", "material_bestellen", "material_anlegen",
    "abwesenheit_melden", "mitarbeiter_zurueck", "wissen_merken", "wissen_loeschen",
    "auftrag_status", "angebot_erstellen", "angebot_senden", "rechnung_erstellen",
    "rechnung_abrechnen", "anfrage_beantworten", "email_schreiben",
    "drive_ordner_anlegen", "drive_notiz_anlegen",
}


def test_registry_has_all_tools():
    namen = {s.name for s in cc._REGISTRY}
    assert namen == _ERWARTETE_TOOLS
    # Namen muessen eindeutig sein — _find_tool() nimmt sonst still das erste.
    assert len(cc._REGISTRY) == len(namen)


# --------------------------------------------------------------------------
# Read-Schleife -> message
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_tool_then_message(monkeypatch):
    called = {}

    async def fake_kunde(ctx, args):
        called["args"] = args
        return {"gespraeche": [{"kunde": "Meier"}], "angebote_anzahl": 1}

    _patch_tool(monkeypatch, "kunde_suchen", fake_kunde)
    models = _patch_gemini(monkeypatch, [
        _resp([_part_fc("kunde_suchen", {"name": "Meier"})]),
        _resp([_part_text("Ich habe Meier gefunden: 1 Angebot.")]),
    ])

    res = await cc.run_command("Was läuft bei Meier?", _ctx())
    assert res["type"] == "message"
    assert "Meier" in res["text"]
    assert called["args"] == {"name": "Meier"}
    assert models.calls == 2  # Read-Ergebnis wurde zurueckgespielt


# --------------------------------------------------------------------------
# Write -> confirm (kein Auto-Execute)
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_write_tool_returns_confirm_without_executing(monkeypatch):
    executed = {"ran": False}

    async def fake_rueckruf(ctx, args):
        executed["ran"] = True
        return {"ok": True}

    _patch_tool(monkeypatch, "rueckruf_anlegen", fake_rueckruf)
    _patch_gemini(monkeypatch, [
        _resp([_part_fc("rueckruf_anlegen",
                        {"kunde_name": "Meier", "kunde_telefon": "0151 222"})]),
    ])

    res = await cc.run_command("Ruf Meier zurück, 0151 222", _ctx())
    assert res["type"] == "confirm"
    assert res["tool"] == "rueckruf_anlegen"
    assert res["args"]["kunde_name"] == "Meier"
    assert "Meier" in res["summary"]
    assert executed["ran"] is False  # NICHT ausgefuehrt


@pytest.mark.asyncio
async def test_execute_confirmed_runs_write(monkeypatch):
    async def fake_rueckruf(ctx, args):
        return {"ok": True, "id": "abc", "kunde": args.get("kunde_name")}

    _patch_tool(monkeypatch, "rueckruf_anlegen", fake_rueckruf)
    res = await cc.execute_confirmed(
        "rueckruf_anlegen", {"kunde_name": "Meier", "kunde_telefon": "0151"}, _ctx())
    assert res["type"] == "done"
    assert res["result"]["ok"] is True
    assert res["result"]["kunde"] == "Meier"


@pytest.mark.asyncio
async def test_execute_confirmed_rejects_gated_tool():
    # Monteur (kein Inhaber) darf abwesenheit_melden nicht ausfuehren.
    res = await cc.execute_confirmed(
        "abwesenheit_melden", {"mitarbeiter": "Tobias", "typ": "krank"},
        _ctx(is_inhaber=False))
    assert res["type"] == "error"
    assert "freigeschaltet" in res["text"].lower()


@pytest.mark.asyncio
async def test_execute_confirmed_rejects_read_tool():
    res = await cc.execute_confirmed("kunde_suchen", {"name": "Meier"}, _ctx())
    assert res["type"] == "error"


# --------------------------------------------------------------------------
# Defensive: unbekanntes Tool
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unknown_tool_is_rejected_defensively(monkeypatch):
    _patch_gemini(monkeypatch, [
        _resp([_part_fc("delete_everything", {})]),
    ])
    res = await cc.run_command("lösch alles", _ctx())
    assert res["type"] == "message"  # defensive Rueckmeldung, kein Crash


@pytest.mark.asyncio
async def test_empty_command_returns_error():
    res = await cc.run_command("   ", _ctx())
    assert res["type"] == "error"


@pytest.mark.asyncio
async def test_no_function_call_returns_message(monkeypatch):
    _patch_gemini(monkeypatch, [
        _resp([_part_text("Wie kann ich helfen?")]),
    ])
    res = await cc.run_command("Hallo", _ctx())
    assert res["type"] == "message"
    assert res["text"] == "Wie kann ich helfen?"


# --------------------------------------------------------------------------
# E-Mail schreiben: Entwurf -> Freigabe -> Versand
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_email_tool_returns_entwurf_without_sending(monkeypatch):
    gesendet = {"ran": False}

    async def fake_send(ctx, args):
        gesendet["ran"] = True
        return {"ok": True}

    _patch_tool(monkeypatch, "email_schreiben", fake_send)
    _patch_gemini(monkeypatch, [
        _resp([_part_fc("email_schreiben", {
            "empfaenger": "meier@example.de", "empfaenger_name": "Frau Meier",
            "betreff": "Termin Donnerstag",
            "text": "Hallo Frau Meier,\n\nwir kommen Donnerstag um 10 Uhr.\n\nViele Grüße"})]),
    ])

    res = await cc.run_command("Schreib Frau Meier wegen Donnerstag", _ctx())
    assert res["type"] == "email_entwurf"
    assert res["tool"] == "email_schreiben"
    assert res["empfaenger"] == "meier@example.de"
    assert res["betreff"] == "Termin Donnerstag"
    assert "Donnerstag" in res["text"]
    assert res["hinweis"] is None
    assert gesendet["ran"] is False  # ohne Freigabe geht nichts raus


@pytest.mark.asyncio
async def test_email_entwurf_ohne_adresse_setzt_hinweis(monkeypatch):
    # Kein empfaenger, kein Kunde im Stamm -> Entwurf trotzdem da, aber
    # mit Hinweis; die Adresse traegt der Nutzer in der Karte nach.
    async def keine_adresse(tid, kunde_name):
        return None

    import core.services.mail_compose as mc
    monkeypatch.setattr(mc, "lookup_kunde_email", keine_adresse)
    _patch_gemini(monkeypatch, [
        _resp([_part_fc("email_schreiben", {
            "kunde_name": "Meier", "betreff": "Rückfrage", "text": "Hallo,\n\nkurze Frage."})]),
    ])
    res = await cc.run_command("Schreib Meier eine Mail", _ctx())
    assert res["type"] == "email_entwurf"
    assert res["empfaenger"] == ""
    assert res["hinweis"]


@pytest.mark.asyncio
async def test_email_versand_erst_nach_freigabe(monkeypatch):
    gesehen = {}

    async def fake_send_freie_mail(tid, **kw):
        gesehen.update(kw)
        return {"ok": True, "to_email": kw["to_email"], "betreff": kw["betreff"],
                "anhaenge": len(kw.get("anhaenge") or [])}

    import core.services.mail_compose as mc
    monkeypatch.setattr(mc, "send_freie_mail", fake_send_freie_mail)

    res = await cc.execute_confirmed("email_schreiben", {
        "empfaenger": "meier@example.de", "betreff": "Termin",
        "text": "Hallo,\n\nbis Donnerstag.",
        "anhaenge": [{"name": "Aufmass.pdf", "mime": "application/pdf", "b64": "aGk="}],
    }, _ctx())

    assert res["type"] == "done"
    assert res["result"]["ok"] is True
    assert gesehen["to_email"] == "meier@example.de"
    assert gesehen["anhaenge"][0]["quelle"] == "upload"


def test_email_tool_ist_write_und_ungegated():
    spec = cc._spec_by_name("email_schreiben")
    assert spec is not None
    assert spec.kind == "write"          # nie Auto-Execute
    assert spec.summarize is not None
    # Mails schreiben darf jeder Mitarbeiter, auch ohne Zusatz-Feature.
    monteur = _ctx(features=(), is_inhaber=False)
    assert "email_schreiben" in {s.name for s in cc._available_tools(monteur)}


@pytest.mark.parametrize("text,erwartet", [
    ("Schreib Henrik eine Mail, dass er morgen kommen kann", True),
    ("Maile dem Kunden die Bestätigung", True),
    ("Schick Frau Meier eine E-Mail", True),
    ("Trag Frau Meier morgen 14 Uhr ein", False),
    ("Zeig mir die offenen Rückrufe", False),
    ("Schreib das in die Wissensdatenbank", False),   # kein Mail-Wort
])
def test_ist_mail_auftrag(text, erwartet):
    assert cc._ist_mail_auftrag(text) is erwartet


@pytest.mark.parametrize("say,erwartet", [
    ("Ich kann Henrik schreiben, dass er morgen kommt. Ist das so in Ordnung?", True),
    ("Soll ich ihm das so schicken?", True),
    ("An welche Adresse soll die Mail gehen?", False),   # echte Rueckfrage
    ("Was soll in der Mail stehen?", False),
    ("Alles klar, erledigt.", False),
])
def test_ist_freigabe_frage(say, erwartet):
    assert cc._ist_freigabe_frage(say) is erwartet


@pytest.mark.asyncio
async def test_mail_ankuendigung_wird_zum_entwurf_nachgefasst(monkeypatch):
    # Gemini kuendigt den Mail-Inhalt als Text an und fragt um Erlaubnis,
    # statt das Tool zu rufen. Genau das will Henrik nicht sehen — die
    # Schleife fasst einmal nach und liefert den fertigen Entwurf.
    models = _patch_gemini(monkeypatch, [
        _resp([_part_text("Ich kann Henrik eine E-Mail schicken, dass er morgen "
                          "vorbeikommen kann. Ist das so in Ordnung?")]),
        _resp([_part_fc("email_schreiben", {
            "empfaenger": "henrik@example.de", "betreff": "Gespräch morgen",
            "text": "Hallo Henrik,\n\nmorgen passt.\n\nViele Grüße"})]),
    ])

    res = await cc.run_command("Schreib Henrik eine Mail wegen morgen", _ctx())
    assert res["type"] == "email_entwurf"
    assert res["betreff"] == "Gespräch morgen"
    assert models.calls == 2


@pytest.mark.asyncio
async def test_echte_rueckfrage_bleibt_rueckfrage(monkeypatch):
    # Fehlt Q wirklich eine Angabe, darf die Nachfass-Regel NICHT greifen —
    # sonst erfindet er den Mail-Inhalt.
    models = _patch_gemini(monkeypatch, [
        _resp([_part_text("An welche Adresse soll die Mail gehen?")]),
    ])
    res = await cc.run_command("Schreib Henrik eine Mail", _ctx())
    assert res["type"] == "message"
    assert models.calls == 1


@pytest.mark.asyncio
async def test_nachfassen_passiert_nur_einmal(monkeypatch):
    models = _patch_gemini(monkeypatch, [
        _resp([_part_text("Ich schreibe ihm, dass es morgen passt. Ist das so in Ordnung?")]),
        _resp([_part_text("Soll ich das wirklich so schicken?")]),
    ])
    res = await cc.run_command("Schreib Henrik eine Mail wegen morgen", _ctx())
    assert res["type"] == "message"
    assert models.calls == 2  # kein Endlos-Nachfassen


# ==========================================================================
# Parallele Tool-Calls in EINEM Gemini-Zug
# ==========================================================================
# Gemini darf pro Zug mehrere Werkzeuge aufrufen. Die API verlangt dann im
# Folgezug GENAU so viele function_response-Parts wie es calls gab — sonst
# 400 INVALID_ARGUMENT ("number of function response parts is equal to the
# number of function call parts") und der Assistent faellt komplett aus.

@pytest.mark.asyncio
async def test_mehrere_read_calls_bekommen_je_eine_antwort(monkeypatch):
    aufgerufen: list[str] = []

    async def fake_termine(ctx, args):
        aufgerufen.append("anstehende_termine")
        return {"termine": []}

    async def fake_rueckrufe(ctx, args):
        aufgerufen.append("offene_rueckrufe")
        return {"rueckrufe": []}

    _patch_tool(monkeypatch, "anstehende_termine", fake_termine)
    _patch_tool(monkeypatch, "offene_rueckrufe", fake_rueckrufe)

    models = _patch_gemini(monkeypatch, [
        _resp([_part_fc("anstehende_termine", {}),
               _part_fc("offene_rueckrufe", {})]),
        _resp([_part_text("Heute ist nichts offen.")]),
    ])

    out = await cc.run_command("Wie ist der Stand heute?", _ctx())
    assert out["type"] == "message"
    assert sorted(aufgerufen) == ["anstehende_termine", "offene_rueckrufe"]

    # Zweiter Gemini-Call: so viele function_response-Parts wie calls
    antwort_content = models.seen_contents[1][-1]
    assert len(antwort_content.parts) == 2


@pytest.mark.asyncio
async def test_write_call_neben_read_call_geht_in_die_bestaetigung(monkeypatch):
    """Ist ein Write-Tool dabei, gewinnt es: der Pfad kehrt sofort mit der
    Bestaetigung zurueck (dann geht nichts an Gemini zurueck)."""
    _patch_gemini(monkeypatch, [
        _resp([_part_fc("anstehende_termine", {}),
               _part_fc("rueckruf_anlegen",
                        {"kunde_name": "Meier", "kunde_telefon": "0651"})]),
    ])
    out = await cc.run_command("Ruf Meier zurueck", _ctx())
    assert out["type"] == "confirm"
    assert out["tool"] == "rueckruf_anlegen"
