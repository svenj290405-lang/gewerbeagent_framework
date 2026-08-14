"""Tests fuer core/services/mail_compose.py — die frei formulierte Mail
aus dem Assistenten.

Reine Unit-Tests: kein Netz, keine DB. Getestet werden Validierung
(Adresse/Betreff/Text), das Normalisieren der Anhang-Angaben, das
Rendern des Bodys (Escaping!) und der Uebergabe-Vertrag an
``send_tracked_mail``.
"""
from __future__ import annotations

import base64
import uuid
from types import SimpleNamespace

import pytest

from core.services import mail_compose as mc


# --------------------------------------------------------------------------
# Validierung
# --------------------------------------------------------------------------

@pytest.mark.parametrize("adresse", [
    "kunde@example.de", "vor.nach+tag@sub.example.co.uk",
])
def test_email_re_akzeptiert_gueltige_adressen(adresse):
    assert mc.EMAIL_RE.match(adresse)


@pytest.mark.parametrize("adresse", [
    "", "kunde", "kunde@", "@example.de", "kunde@example", "a b@example.de",
])
def test_email_re_lehnt_kaputte_adressen_ab(adresse):
    assert not mc.EMAIL_RE.match(adresse)


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs,erwartet", [
    ({"to_email": "kaputt", "betreff": "Hi", "text": "Text"}, "Adresse"),
    ({"to_email": "k@example.de", "betreff": "", "text": "Text"}, "Betreff"),
    ({"to_email": "k@example.de", "betreff": "Hi", "text": ""}, "Text"),
])
async def test_send_freie_mail_validiert_vor_dem_versand(monkeypatch, kwargs, erwartet):
    # Kein Versand-Stub noetig: die Validierung muss vorher greifen.
    res = await mc.send_freie_mail(uuid.uuid4(), **kwargs)
    assert res["ok"] is False
    assert erwartet.lower() in res["error"].lower()


# --------------------------------------------------------------------------
# Anhaenge
# --------------------------------------------------------------------------

def test_normalize_anhaenge_akzeptiert_drive_ids_und_uploads():
    out = mc.normalize_anhaenge([
        "1abcDEF_-234567",                                    # nackte Drive-ID
        {"id": "9zyxWVU-_876543", "name": "Aufmass.pdf"},     # Drive mit Namen
        {"name": "foto.jpg", "mime": "image/jpeg", "b64": "aGk="},
    ])
    assert [a["quelle"] for a in out] == ["drive", "drive", "upload"]
    assert out[1]["name"] == "Aufmass.pdf"
    assert out[2]["mime"] == "image/jpeg"


def test_normalize_anhaenge_wirft_muell_raus():
    out = mc.normalize_anhaenge(["kurz", {"id": "!!!"}, 42, None, {}])
    assert out == []


def test_normalize_anhaenge_begrenzt_die_anzahl():
    viele = [{"name": f"f{i}", "b64": "aGk="} for i in range(20)]
    assert len(mc.normalize_anhaenge(viele)) == mc.MAX_ANHAENGE


@pytest.mark.asyncio
async def test_load_anhaenge_meldet_zu_grosse_datei():
    zu_gross = base64.b64encode(b"x" * (mc.MAX_ANHANG_BYTES + 1)).decode()
    fertig, fehler = await mc.load_anhaenge(uuid.uuid4(), [
        {"quelle": "upload", "name": "riesig.pdf", "mime": "application/pdf",
         "b64": zu_gross}])
    assert fertig == []
    assert fehler and "gross" in fehler[0]


@pytest.mark.asyncio
async def test_load_anhaenge_meldet_kaputtes_base64():
    fertig, fehler = await mc.load_anhaenge(uuid.uuid4(), [
        {"quelle": "upload", "name": "kaputt.pdf", "b64": "###"}])
    assert fertig == []
    assert fehler


@pytest.mark.asyncio
async def test_load_anhaenge_holt_drive_datei(monkeypatch):
    async def fake_bytes(tid, file_id, employee_id=None):
        assert file_id == "1abcDEF_-234567"
        return b"%PDF-1.4", "application/pdf"

    import core.integrations.google_drive as gd
    monkeypatch.setattr(gd, "get_file_bytes", fake_bytes)

    fertig, fehler = await mc.load_anhaenge(uuid.uuid4(), [
        {"quelle": "drive", "id": "1abcDEF_-234567", "name": "Aufmass.pdf"}])
    assert fehler == []
    assert fertig[0]["filename"] == "Aufmass.pdf"
    assert fertig[0]["bytes"] == b"%PDF-1.4"


# --------------------------------------------------------------------------
# Body-Rendering
# --------------------------------------------------------------------------

def test_build_mail_html_escaped_und_baut_absaetze():
    html = mc.build_mail_html(
        "Hallo <b>Herr</b> Meier,\nkurz & knapp.\n\nViele Grüße",
        company_name="Jantos GmbH", contact_name="Sven Jantos",
        contact_email="sven@example.de")
    assert "&lt;b&gt;" in html                      # kein durchgereichtes HTML
    assert "<b>Herr</b>" not in html
    assert "&amp; knapp" in html
    assert html.count("<p style=\"margin:0 0 16px;\">") == 2   # zwei Absaetze
    assert "kurz &amp; knapp" in html and "<br>" in html       # Umbruch im Absatz
    assert "Jantos GmbH" in html and "sven@example.de" in html


def test_build_mail_text_haengt_kontakt_an():
    txt = mc.build_mail_text("Hallo,\n\nbis Donnerstag.",
                             contact_name="Sven Jantos", contact_phone="0201 1234")
    assert txt.startswith("Hallo,")
    assert "Sven Jantos" in txt and "0201 1234" in txt


# --------------------------------------------------------------------------
# Absender-Konsistenz
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_footer_zeigt_das_postfach_nicht_die_stammdaten(monkeypatch):
    """Der Footer muss die Adresse tragen, aus der wirklich gesendet wird.

    Steht dort eine andere als im From-Feld, ist das ein Spamfilter-Merkmal
    — und eine Kunden-Antwort dorthin liest der Inbox-Poller nie.
    """
    tid = uuid.uuid4()
    _patch_tenant(monkeypatch, SimpleNamespace(
        id=tid, company_name="Schreiberei Jantos", contact_name="Sven Jantos",
        contact_email="veraltet@example.com", contact_phone="0201 1234"))

    gesehen = {}

    async def fake_tracked(**kw):
        gesehen.update(kw)
        return {"success": True, "internet_message_id": "<x@outlook.de>"}

    async def fake_mailbox(tid_, employee_id=None):
        return "svenjantos@outlook.de"

    import core.integrations.microsoft as ms
    import core.utils.mail_absender as absender
    monkeypatch.setattr(ms, "send_tracked_mail", fake_tracked)
    monkeypatch.setattr(absender, "mailbox_adresse", fake_mailbox)

    res = await mc.send_freie_mail(
        tid, to_email="kunde@example.de", betreff="Ihr Angebot",
        text="Hallo,\n\nanbei das Angebot.")

    assert res["ok"] is True
    assert "svenjantos@outlook.de" in gesehen["body_html"]
    assert "svenjantos@outlook.de" in gesehen["body_text"]
    assert "veraltet@example.com" not in gesehen["body_html"]


@pytest.mark.asyncio
async def test_footer_faellt_ohne_postfach_auf_die_stammdaten_zurueck(monkeypatch):
    tid = uuid.uuid4()
    _patch_tenant(monkeypatch, SimpleNamespace(
        id=tid, company_name="X", contact_name="Sven", contact_phone="",
        contact_email="kontakt@example.de"))

    gesehen = {}

    async def fake_tracked(**kw):
        gesehen.update(kw)
        return {"success": True}

    async def fake_mailbox(tid_, employee_id=None):
        return None  # Microsoft (noch) nicht verbunden

    import core.integrations.microsoft as ms
    import core.utils.mail_absender as absender
    monkeypatch.setattr(ms, "send_tracked_mail", fake_tracked)
    monkeypatch.setattr(absender, "mailbox_adresse", fake_mailbox)

    await mc.send_freie_mail(
        tid, to_email="kunde@example.de", betreff="Hi", text="Text")
    assert "kontakt@example.de" in gesehen["body_html"]


# --------------------------------------------------------------------------
# Versand-Vertrag
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_freie_mail_uebergibt_alles_an_graph(monkeypatch):
    tid = uuid.uuid4()
    tenant = SimpleNamespace(
        id=tid, company_name="Jantos GmbH", contact_name="Sven Jantos",
        contact_email="sven@example.de", contact_phone="0201 1234")
    gesehen = {}

    async def fake_tracked(**kw):
        gesehen.update(kw)
        return {"success": True, "internet_message_id": "<abc@outlook>"}

    _patch_tenant(monkeypatch, tenant)
    import core.integrations.microsoft as ms
    monkeypatch.setattr(ms, "send_tracked_mail", fake_tracked)

    res = await mc.send_freie_mail(
        tid, to_email="meier@example.de", to_name="Frau Meier",
        betreff="Termin Donnerstag", text="Hallo Frau Meier,\n\nbis dann.",
        anhaenge=[{"quelle": "upload", "name": "info.txt", "mime": "text/plain",
                   "b64": base64.b64encode(b"hallo").decode()}])

    assert res["ok"] is True
    assert res["to_email"] == "meier@example.de"
    assert res["anhaenge"] == 1
    assert gesehen["subject"] == "Termin Donnerstag"
    assert gesehen["to_email"] == "meier@example.de"
    assert gesehen["attachments"][0]["bytes"] == b"hallo"
    assert "bis dann." in gesehen["body_text"]


@pytest.mark.asyncio
async def test_send_freie_mail_ohne_microsoft_gibt_klaren_hinweis(monkeypatch):
    tid = uuid.uuid4()
    _patch_tenant(monkeypatch, SimpleNamespace(
        id=tid, company_name="Jantos GmbH", contact_name="", contact_email="",
        contact_phone=""))

    async def fake_tracked(**kw):
        return {"success": False, "error": "Microsoft nicht verbunden"}

    import core.integrations.microsoft as ms
    monkeypatch.setattr(ms, "send_tracked_mail", fake_tracked)

    res = await mc.send_freie_mail(
        tid, to_email="meier@example.de", betreff="Hi", text="Text")
    assert res["ok"] is False
    assert "Verbindungen" in res["error"]


@pytest.mark.asyncio
async def test_send_freie_mail_bricht_bei_kaputtem_anhang_ab(monkeypatch):
    tid = uuid.uuid4()
    _patch_tenant(monkeypatch, SimpleNamespace(
        id=tid, company_name="X", contact_name="", contact_email="", contact_phone=""))
    gerufen = {"versand": False}

    async def fake_tracked(**kw):
        gerufen["versand"] = True
        return {"success": True}

    import core.integrations.microsoft as ms
    monkeypatch.setattr(ms, "send_tracked_mail", fake_tracked)

    res = await mc.send_freie_mail(
        tid, to_email="meier@example.de", betreff="Hi", text="Text",
        anhaenge=[{"quelle": "upload", "name": "kaputt.pdf", "b64": "###"}])
    assert res["ok"] is False
    assert gerufen["versand"] is False  # lieber nichts als eine Mail ohne Anhang


# --------------------------------------------------------------------------
# Helfer
# --------------------------------------------------------------------------

def _patch_tenant(monkeypatch, tenant):
    """Ersetzt den Tenant-Load durch eine Session-Attrappe."""
    class _Result:
        def scalar_one_or_none(self):
            return tenant

    class _Session:
        async def execute(self, *a, **kw):
            return _Result()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    import core.database.connection as conn
    monkeypatch.setattr(conn, "get_session", lambda: _Session())
