"""Tests fuer die Zustellbarkeit ausgehender Mails.

Drei Dinge, die zusammen darueber entscheiden, ob eine Q-Mail beim Kunden
im Posteingang oder im Spam-Ordner landet:

1. **Threading-Header** — eine Antwort mit "Re:" im Betreff, aber ohne
   ``In-Reply-To``, ist fuer den Client eine lose Mail und fuer den Filter
   eine unbelegte Behauptung. Nur der MIME-Pfad kann diese Header setzen.
2. **Anhaenge im selben Umschlag** — sonst faellt der Versand auf Graphs
   JSON-Draft zurueck, und damit fallen Text-Teil UND Threading weg.
3. **Freemail-Erkennung** — Grundlage der Warnung in den Verbindungen.

Reine Unit-Tests: kein Netz, keine DB (die Graph-Aufrufe laufen gegen
einen Fake-httpx-Client).
"""
from __future__ import annotations

import base64
import uuid
from email import message_from_bytes

import pytest

import core.integrations.microsoft as ms
from core.utils.mail_absender import ist_freemail_adresse


def _mime(**kw):
    """Baut die MIME und gibt sie als geparste Message zurueck."""
    roh = ms._build_mime_b64(**kw)
    return message_from_bytes(base64.b64decode(roh))


# --------------------------------------------------------------------------
# Threading-Header
# --------------------------------------------------------------------------

def test_mime_setzt_in_reply_to_und_references():
    msg = _mime(
        subject="Re: Anfrage Kuechenzeile", to_email="kunde@example.de",
        body_html="<p>Hallo</p>", body_text="Hallo",
        in_reply_to="<kunde-123@example.de>",
    )
    assert msg["In-Reply-To"] == "<kunde-123@example.de>"
    # Ohne bekannte Kette ist der direkte Vorgaenger die korrekte
    # einelementige References.
    assert msg["References"] == "<kunde-123@example.de>"


def test_mime_uebernimmt_vorhandene_references_kette():
    msg = _mime(
        subject="Re: X", to_email="kunde@example.de",
        body_html="<p>Hallo</p>", body_text="Hallo",
        in_reply_to="<zwei@example.de>",
        references="<eins@example.de> <zwei@example.de>",
    )
    assert msg["References"] == "<eins@example.de> <zwei@example.de>"


def test_mime_ohne_threading_bleibt_ohne_header():
    msg = _mime(
        subject="Ihr Angebot", to_email="kunde@example.de",
        body_html="<p>Hallo</p>", body_text="Hallo",
    )
    assert msg["In-Reply-To"] is None
    assert msg["References"] is None


def test_mime_setzt_keinen_absender():
    """From setzt Graph selbst aufs Postfach — schreiben wir eins rein,
    weicht es garantiert irgendwann vom echten Postfach ab."""
    msg = _mime(
        subject="Hi", to_email="kunde@example.de",
        body_html="<p>Hallo</p>", body_text="Hallo",
    )
    assert msg["From"] is None


# --------------------------------------------------------------------------
# Aufbau: text vor html, Anhaenge aussen herum
# --------------------------------------------------------------------------

def test_mime_ohne_anhang_ist_alternative_mit_text_zuerst():
    msg = _mime(
        subject="Hi", to_email="kunde@example.de",
        body_html="<p>Hallo</p>", body_text="Hallo",
    )
    assert msg.get_content_type() == "multipart/alternative"
    teile = [t.get_content_type() for t in msg.get_payload()]
    # In multipart/alternative gilt der LETZTE Teil als bevorzugt.
    assert teile == ["text/plain", "text/html"]


def test_mime_mit_anhang_wickelt_mixed_um_das_alternative():
    msg = _mime(
        subject="Ihr Angebot", to_email="kunde@example.de",
        body_html="<p>Hallo</p>", body_text="Hallo",
        in_reply_to="<kunde-1@example.de>",
        attachments=[{"filename": "Angebot.pdf", "bytes": b"%PDF-1.4 xx",
                      "content_type": "application/pdf"}],
    )
    assert msg.get_content_type() == "multipart/mixed"
    teile = msg.get_payload()
    assert teile[0].get_content_type() == "multipart/alternative"
    assert teile[1].get_filename() == "Angebot.pdf"
    assert teile[1].get_payload(decode=True) == b"%PDF-1.4 xx"
    # Anhang darf das Threading nicht kosten — genau das war vorher der Fall.
    assert msg["In-Reply-To"] == "<kunde-1@example.de>"


def test_mime_ueberspringt_anhang_ohne_bytes():
    msg = _mime(
        subject="Hi", to_email="kunde@example.de",
        body_html="<p>H</p>", body_text="H",
        attachments=[{"filename": "leer.pdf", "bytes": None}],
    )
    assert len(msg.get_payload()) == 1


# --------------------------------------------------------------------------
# Groessen-Grenze (Graph nimmt MIME-Drafts nur bis 4 MB)
# --------------------------------------------------------------------------

def test_kleine_anhaenge_passen_in_die_mime():
    assert ms._mime_vertraegt_anhaenge(None) is True
    assert ms._mime_vertraegt_anhaenge([{"bytes": b"x" * 100_000}]) is True


def test_grosse_anhaenge_fallen_auf_den_json_pfad_zurueck():
    # 3 MB roh sind base64 rund 4 MB — darueber lehnt Graph den MIME-Draft ab.
    assert ms._mime_vertraegt_anhaenge([{"bytes": b"x" * 3_000_000}]) is False


# --------------------------------------------------------------------------
# send_tracked_mail: Pfadwahl + keine doppelten Anhaenge
# --------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code=201, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = ""

    def json(self):
        return self._payload


class _FakeClient:
    """Minimaler httpx-Ersatz: merkt sich alle POSTs."""

    def __init__(self, protokoll, **kw):
        self._protokoll = protokoll

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None, content=None):
        self._protokoll.append({"url": url, "json": json, "content": content})
        if url.endswith("/messages"):
            return _FakeResponse(201, {
                "id": "draft-1",
                "internetMessageId": "<neu@outlook.de>",
                "conversationId": "conv-1",
            })
        if url.endswith("/attachments"):
            return _FakeResponse(201)
        return _FakeResponse(202)  # /send

    async def delete(self, url, headers=None):
        return _FakeResponse(204)


@pytest.fixture
def graph(monkeypatch):
    protokoll: list[dict] = []

    async def fake_token(tenant_id, employee_id=None):
        return "token-xyz"

    monkeypatch.setattr(ms, "get_microsoft_token", fake_token)
    monkeypatch.setattr(
        ms.httpx, "AsyncClient",
        lambda **kw: _FakeClient(protokoll, **kw))
    return protokoll


@pytest.mark.asyncio
async def test_send_tracked_mail_schickt_anhang_genau_einmal(graph):
    res = await ms.send_tracked_mail(
        tenant_id=uuid.uuid4(), to_email="kunde@example.de",
        subject="Re: Anfrage", body_html="<p>Hallo</p>", body_text="Hallo",
        attachments=[{"filename": "Angebot.pdf", "bytes": b"%PDF",
                      "content_type": "application/pdf"}],
        in_reply_to="<kunde-1@example.de>",
    )
    assert res["success"] is True

    # Draft kam als MIME (content statt json), und es gab KEINEN
    # zusaetzlichen POST auf /attachments — sonst haenge der Anhang doppelt.
    draft = graph[0]
    assert draft["json"] is None and draft["content"]
    assert not any("/attachments" in p["url"] for p in graph)

    msg = message_from_bytes(base64.b64decode(draft["content"]))
    assert msg["In-Reply-To"] == "<kunde-1@example.de>"
    assert msg.get_content_type() == "multipart/mixed"


@pytest.mark.asyncio
async def test_send_tracked_mail_faellt_bei_grossem_anhang_auf_json(graph):
    res = await ms.send_tracked_mail(
        tenant_id=uuid.uuid4(), to_email="kunde@example.de",
        subject="Re: Anfrage", body_html="<p>Hallo</p>", body_text="Hallo",
        attachments=[{"filename": "Plan.pdf", "bytes": b"x" * 3_000_000,
                      "content_type": "application/pdf"}],
        in_reply_to="<kunde-1@example.de>",
    )
    assert res["success"] is True
    # JSON-Draft + separater Anhang-POST; Threading geht dabei verloren
    # (Graph laesst ueber internetMessageHeaders nur x-Header zu).
    assert graph[0]["json"] is not None
    assert any("/attachments" in p["url"] for p in graph)


@pytest.mark.asyncio
async def test_send_tracked_mail_ohne_body_text_bleibt_json(graph):
    await ms.send_tracked_mail(
        tenant_id=uuid.uuid4(), to_email="kunde@example.de",
        subject="Hi", body_html="<p>Hallo</p>",
    )
    assert graph[0]["json"] is not None


# --------------------------------------------------------------------------
# Freemail-Erkennung (Grundlage der Warnung in den Verbindungen)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("adresse", [
    "svenjantos@outlook.de", "info@gmx.de", "chef@web.de",
    "Info@GMAIL.com", " kontakt@t-online.de ",
])
def test_freemail_wird_erkannt(adresse):
    assert ist_freemail_adresse(adresse) is True


@pytest.mark.parametrize("adresse", [
    "info@schreiberei-jantos.de", "sven@jantos-gmbh.com", "", None,
    "kaputt", "info@outlook.de.example.com",
])
def test_eigene_domain_loest_keine_warnung_aus(adresse):
    assert ist_freemail_adresse(adresse) is False
