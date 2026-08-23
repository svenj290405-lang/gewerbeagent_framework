"""Tests fuer den Kundengespraech-Bereich der PWA.

Schwerpunkt ist die Vertraulichkeitsgrenze: was der Handwerker fuer sich
festhaelt (Transkript, Handnotiz, To-dos), darf NIE in der Kundenmail
landen. Der Test prueft das an der Stelle, an der es kippen wuerde — im
Prompt, den Gemini zu sehen bekommt.

Dazu die Endpunkte: Gespraech anlegen, Detail, Notiz, Bild-Zuordnung.
Reine Unit-Tests mit Fakes, keine echte DB (Muster wie test_app_auftraege).
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from core.api import app_screens
from core.services import kundengespraech as ksvc


def _req(body=None, tenant_id=None, company="Schreinerei Test"):
    req = SimpleNamespace()

    async def _json():
        if body is None:
            raise ValueError("kein body")
        return body
    req.json = _json
    req.state = SimpleNamespace(
        app_tenant=SimpleNamespace(id=tenant_id or uuid.uuid4(),
                                   company_name=company))
    return req


def _json_body(resp):
    return json.loads(bytes(resp.body))


def _gespraech(**kw):
    basis = dict(
        id=uuid.uuid4(), kunde_name="Müller", kunde_id=None,
        briefing_kurz="Bad neu fliesen, Kunde will graue Fliesen.",
        notizen_lang="Fliesen 30x60, Zuschnitt an der Dusche.",
        handnotiz="ACHTUNG: zahlt schleppend, Vorkasse verlangen!",
        raw_transcript="also der Müller sagt er zahlt nie puenktlich",
        todos=["Material bestellen", "Aufmass nachholen"],
        termin_datum=None, termin_ort=None, audio_dauer_sekunden=None,
        gespraech_datum=None, confidence="hoch",
    )
    basis.update(kw)
    return SimpleNamespace(**basis)


# =====================================================================
# Vertraulichkeit: was Gemini fuer die Kundenmail sehen darf
# =====================================================================

@pytest.mark.asyncio
async def test_kundenmail_sieht_weder_transkript_noch_handnotiz(monkeypatch):
    """Der Kern der ganzen Funktion: interne Sachen gehen nicht raus.

    Geprueft wird nicht die Formulierung der KI (die ist nicht
    deterministisch), sondern dass das Vertrauliche gar nicht erst im
    Prompt steht — was Gemini nie sieht, kann es auch nicht ausplaudern.
    """
    gesehen = {}
    import core.ai.gemini as gemini

    async def _fake_gemini(prompt, **kw):
        gesehen["prompt"] = prompt
        return "Guten Tag Herr Müller, wie besprochen fliesen wir Ihr Bad."
    monkeypatch.setattr(gemini, "call_gemini", _fake_gemini)

    import core.services.mail_compose as mc

    async def _lookup(tid, name):
        return "mueller@example.de"
    monkeypatch.setattr(mc, "lookup_kunde_email", _lookup)

    g = _gespraech()
    entwurf = await ksvc.baue_kundenmail(
        uuid.uuid4(), gespraech=g, bilder=[], betrieb="Schreinerei Test")

    prompt = gesehen["prompt"]
    assert "zahlt schleppend" not in prompt        # Handnotiz
    assert "Vorkasse" not in prompt
    assert "zahlt nie puenktlich" not in prompt    # Transkript
    assert "Material bestellen" not in prompt      # To-dos
    # Was drin sein MUSS, damit die Mail Substanz hat:
    assert "Bad neu fliesen" in prompt
    assert "Fliesen 30x60" in prompt
    # und der Preis-Riegel steht als Anweisung drin
    assert "KEINE Preise" in prompt

    assert entwurf["type"] == "email_entwurf"
    assert entwurf["empfaenger"] == "mueller@example.de"
    assert "Müller" in entwurf["text"]


@pytest.mark.asyncio
async def test_kundenmail_faellt_auf_text_zurueck_wenn_gemini_ausfaellt(monkeypatch):
    import core.ai.gemini as gemini

    async def _boom(prompt, **kw):
        raise RuntimeError("Vertex down")
    monkeypatch.setattr(gemini, "call_gemini", _boom)

    import core.services.mail_compose as mc

    async def _lookup(tid, name):
        return ""
    monkeypatch.setattr(mc, "lookup_kunde_email", _lookup)

    entwurf = await ksvc.baue_kundenmail(
        uuid.uuid4(), gespraech=_gespraech(), bilder=[], betrieb="Schreinerei Test")
    # Entwurf steht trotzdem — der Handwerker redigiert ohnehin.
    assert entwurf["text"]
    assert "Bad neu fliesen" in entwurf["text"]
    # Ohne Adresse bekommt er einen Hinweis statt einer stillen Leerstelle.
    assert "Adresse" in (entwurf["hinweis"] or "")
    # Auch der Fallback traegt nichts Internes nach draussen.
    assert "Vorkasse" not in entwurf["text"]
    assert "zahlt nie puenktlich" not in entwurf["text"]


@pytest.mark.asyncio
async def test_kundenmail_meldet_nicht_anhaengbare_bilder(monkeypatch):
    import core.ai.gemini as gemini

    async def _fake(prompt, **kw):
        return "Text."
    monkeypatch.setattr(gemini, "call_gemini", _fake)
    import core.services.mail_compose as mc

    async def _lookup(tid, name):
        return "a@b.de"
    monkeypatch.setattr(mc, "lookup_kunde_email", _lookup)

    # Bild ohne Drive-ID und ohne Visualisierung -> nicht anhaengbar
    bild = SimpleNamespace(id=uuid.uuid4(), typ="foto", drive_file_id=None,
                           drive_url=None, visualisierung_id=None,
                           dateiname="kaputt.jpg")
    entwurf = await ksvc.baue_kundenmail(
        uuid.uuid4(), gespraech=_gespraech(), bilder=[bild], betrieb="X")
    assert entwurf["anhaenge"] == []
    assert "nicht angehängt" in (entwurf["hinweis"] or "")


# =====================================================================
# Endpunkte
# =====================================================================

@pytest.mark.asyncio
async def test_gespraech_neu_braucht_kunden():
    resp = await app_screens.api_gespraech_neu(
        request=_req({"kunde_name": " "}), emp=SimpleNamespace(id=uuid.uuid4()), _c=None)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_gespraech_detail_invalid_id():
    resp = await app_screens.api_gespraech_detail(
        gespraech_id="keine-uuid", request=_req(), _e=None)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_gespraech_detail_not_found(monkeypatch):
    async def _laden(tid, gid):
        return None, []
    monkeypatch.setattr(app_screens, "_gespraech_laden", _laden)
    resp = await app_screens.api_gespraech_detail(
        gespraech_id=str(uuid.uuid4()), request=_req(), _e=None)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_gespraech_mail_nimmt_nur_gewaehlte_bilder(monkeypatch):
    """Angehakt wird pro Bild — was nicht angehakt ist, geht nicht mit."""
    gid = uuid.uuid4()
    b1 = SimpleNamespace(id=uuid.uuid4(), typ="foto")
    b2 = SimpleNamespace(id=uuid.uuid4(), typ="foto")

    async def _laden(tid, g):
        return _gespraech(id=gid), [b1, b2]
    monkeypatch.setattr(app_screens, "_gespraech_laden", _laden)

    bekommen = {}

    async def _baue(tid, *, gespraech, bilder, betrieb, employee_id=None):
        bekommen["bilder"] = bilder
        bekommen["betrieb"] = betrieb
        return {"type": "email_entwurf", "text": "x"}
    monkeypatch.setattr(ksvc, "baue_kundenmail", _baue)

    resp = await app_screens.api_gespraech_mail(
        gespraech_id=str(gid),
        request=_req({"bild_ids": [str(b2.id)]}),
        emp=SimpleNamespace(id=uuid.uuid4()), _c=None)
    assert resp.status_code == 200
    assert bekommen["bilder"] == [b2]
    assert bekommen["betrieb"] == "Schreinerei Test"


# =====================================================================
# Abschluss: einpflegen statt liegenlassen
# =====================================================================

class _FakeObjSession:
    """Session-Attrappe: liefert immer dasselbe Objekt zurueck."""

    def __init__(self, obj):
        self.obj = obj
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: self.obj)

    def expunge(self, obj):
        """Der Abschluss loest das Gespraech aus der Session, um damit
        ausserhalb weiterzuarbeiten — hier ein No-op."""

    async def commit(self):
        self.committed = True


def test_protokoll_zeigt_internes_getrennt_und_ohne_transkript():
    """Der Drive-Ordner gehoert dem Betrieb — die Handnotiz darf rein,
    aber sichtbar abgesetzt. Das Roh-Transkript bleibt draussen: im Drive
    wuerde es die Aufbewahrungsfrist ueberleben."""
    from core.services.gespraech_abschluss import protokoll_html

    html = protokoll_html({
        "kunde": "Müller", "datum": "31.07.2026 um 09:00 Uhr",
        "briefing": "Bad neu fliesen.", "notizen": "Fliesen 30x60.",
        "todos": ["Material bestellen"],
        "handnotiz": "zahlt schleppend, Vorkasse",
        "transkript": "also der Müller sagt er zahlt nie puenktlich",
        "bilder": ["foto.jpg"],
    })
    assert "Intern — nicht an den Kunden" in html
    assert "Vorkasse" in html
    assert "Material bestellen" in html
    assert "zahlt nie puenktlich" not in html


def test_protokoll_laesst_leere_abschnitte_weg():
    from core.services.gespraech_abschluss import protokoll_html

    html = protokoll_html({"kunde": "Müller", "briefing": "Nur das."})
    assert "Zusammenfassung" in html
    assert "Intern" not in html
    assert "To-dos" not in html


@pytest.mark.asyncio
async def test_abschluss_gilt_auch_ohne_drive(monkeypatch):
    """Ohne Drive-Verbindung ist das Gespraech trotzdem eingepflegt —
    ein Drive-Ausfall darf den Arbeitsablauf nicht blockieren."""
    from core.services import gespraech_abschluss as abschluss
    from core.models.kundengespraech import (
        KUNDENGESPRAECH_STATUS_ABGESCHLOSSEN)

    kunde_id = uuid.uuid4()
    row = SimpleNamespace(
        id=uuid.uuid4(), kunde_name="Müller", kunde_id=None, status="erfasst",
        gespraech_datum=None, termin_datum=None, termin_ort=None,
        audio_dauer_sekunden=None, briefing_kurz="", notizen_lang="",
        todos=None, handnotiz=None, abgeschlossen_am=None,
        protokoll_drive_file_id=None, protokoll_drive_url=None)
    sess = _FakeObjSession(row)
    monkeypatch.setattr(abschluss, "get_session", lambda: sess)

    async def _kunde(g, tid):
        return {"kunde_id": kunde_id, "kunde_neu": True,
                "email": "", "telefon": "", "adresse": ""}
    monkeypatch.setattr(abschluss, "_kunde_sicherstellen", _kunde)

    async def _bilder(tid, g, emp):
        raise ValueError("Google Drive nicht verbunden")
    monkeypatch.setattr(abschluss, "_bilder_sichern", _bilder)

    ergebnis = await abschluss.schliesse_gespraech_ab(
        uuid.uuid4(), row.id, employee_id=uuid.uuid4())
    assert ergebnis["ok"] is True
    assert ergebnis["kunde_neu"] is True
    assert "Drive" in (ergebnis["hinweis"] or "")
    assert row.status == KUNDENGESPRAECH_STATUS_ABGESCHLOSSEN
    assert row.abgeschlossen_am is not None


@pytest.mark.asyncio
async def test_abschluss_ist_idempotent(monkeypatch):
    """Zweimal tippen legt kein zweites Protokoll in den Ordner."""
    from core.services import gespraech_abschluss as abschluss
    from core.models.kundengespraech import (
        KUNDENGESPRAECH_STATUS_ABGESCHLOSSEN)

    row = SimpleNamespace(
        id=uuid.uuid4(), kunde_name="Müller", kunde_id=uuid.uuid4(),
        status=KUNDENGESPRAECH_STATUS_ABGESCHLOSSEN,
        protokoll_drive_url="https://drive/protokoll")
    monkeypatch.setattr(abschluss, "get_session",
                        lambda: _FakeObjSession(row))

    async def _darf_nicht(*a, **kw):
        raise AssertionError("Abschluss lief ein zweites Mal durch")
    monkeypatch.setattr(abschluss, "_kunde_sicherstellen", _darf_nicht)

    ergebnis = await abschluss.schliesse_gespraech_ab(uuid.uuid4(), row.id)
    assert ergebnis["ok"] is True
    assert ergebnis["bereits"] is True
    assert ergebnis["protokoll_url"] == "https://drive/protokoll"


@pytest.mark.asyncio
async def test_verwerfen_blendet_aus_statt_zu_loeschen(monkeypatch):
    from core.models.kundengespraech import KUNDENGESPRAECH_STATUS_ABGELEHNT

    row = SimpleNamespace(status="erfasst")
    sess = _FakeObjSession(row)
    monkeypatch.setattr(app_screens, "get_session", lambda: sess)
    resp = await app_screens.api_gespraech_verwerfen(
        gespraech_id=str(uuid.uuid4()), request=_req(), _e=None, _c=None)
    assert resp.status_code == 200
    assert row.status == KUNDENGESPRAECH_STATUS_ABGELEHNT
    assert sess.committed is True


@pytest.mark.asyncio
async def test_eingepflegtes_gespraech_laesst_sich_nicht_verwerfen(monkeypatch):
    from core.models.kundengespraech import (
        KUNDENGESPRAECH_STATUS_ABGESCHLOSSEN)

    row = SimpleNamespace(status=KUNDENGESPRAECH_STATUS_ABGESCHLOSSEN)
    monkeypatch.setattr(app_screens, "get_session",
                        lambda: _FakeObjSession(row))
    resp = await app_screens.api_gespraech_verwerfen(
        gespraech_id=str(uuid.uuid4()), request=_req(), _e=None, _c=None)
    assert resp.status_code == 409
    assert row.status == KUNDENGESPRAECH_STATUS_ABGESCHLOSSEN


# =====================================================================
# Geplante Gespraeche (Kalender)
# =====================================================================

def test_kunde_aus_termin_betreff():
    """Gebuchte Termine heissen "[Betrieb] Anliegen - Kunde"."""
    assert app_screens._kunde_aus_betreff(
        "[Schreinerei Test] Badsanierung - Hans Müller") == "Hans Müller"
    assert app_screens._kunde_aus_betreff("Aufmass – Frau Meier") == "Frau Meier"
    # Kein Trenner: der ganze Betreff ist der beste Vorschlag, den wir haben.
    assert app_screens._kunde_aus_betreff("Werkstatt aufräumen") == "Werkstatt aufräumen"
    assert app_screens._kunde_aus_betreff("") == ""


def test_kalender_termin_behaelt_die_wanduhrzeit():
    """termin_datum traegt projektweit die lokale Wanduhrzeit mit
    UTC-Etikett — sonst zeigt die App im Sommer zwei Stunden zu frueh."""
    wert = app_screens._kalender_termin_parsen("2026-08-03T14:00:00")
    assert wert is not None
    assert (wert.hour, wert.minute) == (14, 0)
    assert wert.tzinfo is not None
    assert app_screens._kalender_termin_parsen("") is None
    assert app_screens._kalender_termin_parsen(None) is None


@pytest.mark.asyncio
async def test_geplante_termine_ueberspringen_vergangenes(monkeypatch):
    """Nur was noch kommt — Vergangenes steht in der Historie."""
    import datetime as _dt

    jetzt = _dt.datetime.now()

    class _Adapter:
        # Der Bildschirm holt den ganzen Zeitraum in EINEM Aufruf — ein
        # Abruf pro Tag lief bei Microsoft in die Drosselung (HTTP 429)
        # und liess Tage still verschwinden.
        async def list_events_for_range(self, von, bis):
            return [
                {"start_dt": jetzt - _dt.timedelta(hours=2), "subject": "vorbei",
                 "event_id": "alt", "location": ""},
                {"start_dt": jetzt + _dt.timedelta(hours=2), "subject": "[B] Bad - Müller",
                 "event_id": "neu", "location": "Hauptstr. 1"},
            ]

    import plugins.kalender.adapters as adapters

    async def _adapter(tid, emp=None, fallback_calendar_id="primary"):
        return _Adapter()
    monkeypatch.setattr(adapters, "get_calendar_adapter", _adapter)

    events = await app_screens._geplante_kalendertermine(
        uuid.uuid4(), uuid.uuid4(), tage=1)
    assert [e["event_id"] for e in events] == ["neu"]
    assert events[0]["ort"] == "Hauptstr. 1"


@pytest.mark.asyncio
async def test_geplante_liste_zeigt_termine_mit_gespraech_nur_einmal(monkeypatch):
    """Laeuft zu einem Kalendertermin schon ein Gespraech, ist der Termin
    kein Vorschlag mehr — sonst legt man dasselbe Gespraech zweimal an."""
    import datetime as _dt

    gid = uuid.uuid4()
    termin = _dt.datetime.now() + _dt.timedelta(hours=3)
    row = SimpleNamespace(
        id=gid, kunde_name="Müller", termin_datum=termin, termin_ort="",
        kalender_event_id="evt-1", status="erfasst")

    class _Sess:
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False
        async def execute(self, stmt):
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: [row]))
    monkeypatch.setattr(app_screens, "get_session", lambda: _Sess())

    async def _kalender(tid, emp, tage):
        return [
            {"start": termin, "event_id": "evt-1", "titel": "[B] Bad - Müller", "ort": ""},
            {"start": termin, "event_id": "evt-2", "titel": "[B] Dach - Meier", "ort": ""},
        ]
    monkeypatch.setattr(app_screens, "_geplante_kalendertermine", _kalender)

    resp = await app_screens.api_gespraeche_geplant(
        request=_req(), emp=SimpleNamespace(id=uuid.uuid4()))
    geplant = _json_body(resp)["geplant"]
    assert [x["quelle"] for x in geplant] == ["gespraech", "kalender"]
    assert geplant[0]["id"] == str(gid)
    assert geplant[1]["kunde"] == "Meier"


def test_datei_zeile_verlinkt_je_nach_herkunft():
    """Foto kommt aus Drive (Proxy), Visualisierung aus der DB."""
    vid = uuid.uuid4()
    foto = SimpleNamespace(id=uuid.uuid4(), typ="foto", drive_file_id="abc123",
                           drive_url="https://drive/x", visualisierung_id=None,
                           dateiname="foto.jpg")
    viz = SimpleNamespace(id=uuid.uuid4(), typ="visualisierung", drive_file_id=None,
                          drive_url=None, visualisierung_id=vid, dateiname=None)
    z_foto = app_screens._datei_zeile(foto)
    z_viz = app_screens._datei_zeile(viz)
    assert z_foto["url"] == "/app/api/archiv/datei/abc123"
    assert z_viz["url"] == f"/app/api/visualisierungen/{vid}/bild"
    assert z_viz["name"] == "Visualisierung"
