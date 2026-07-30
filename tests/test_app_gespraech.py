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
