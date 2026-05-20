"""Tests fuer Mail-Rendering (Plain-Text-Varianten + multipart/alternative)
und Reply-Trimming.

- Plain-Text-Variante je Template (kein HTML, klare Struktur)
- multipart/alternative-MIME korrekt aufgebaut (text zuerst, html zuletzt)
- Reply-Trimming fuer GMX-, Outlook-, Gmail-, Apple-Quote + Edge-Cases
"""
from __future__ import annotations

import base64
from email import message_from_bytes

from core.integrations.mail_template import build_kunde_reply_text
from core.integrations.mail_pipeline import (
    _build_storno_text, _build_verschiebung_text, _build_buche_confirmation_text,
)
from core.integrations.microsoft import _build_mime_alternative_b64
from core.utils.mail_reply import trim_quoted_reply


# ===================================================================
# Plain-Text-Varianten je Template
# ===================================================================

def test_kunde_reply_text_basic_no_html():
    txt = build_kunde_reply_text(
        kunde_anrede_name="Sven",
        reply_text="Hallo Sven,\nDanke für die Anfrage.\nViele Grüße",
        form_url="https://gewerbeagent.de/anfrage/abc",
        company_name="PURA Tischler", contact_name="Daniel",
        contact_phone="0211 1234", with_formular_button=True,
    )
    assert "<" not in txt and ">" not in txt          # keine HTML-Tags
    assert txt.startswith("Hallo Sven,")              # eigene Anrede
    assert "Danke für die Anfrage." in txt
    assert "Viele Grüße" not in txt                   # LLM-Signatur entfernt
    assert "https://gewerbeagent.de/anfrage/abc" in txt  # Form-URL im Text


def test_kunde_reply_text_form_url_only_with_button():
    txt = build_kunde_reply_text(
        kunde_anrede_name="", reply_text="Gerne.", form_url="https://x/anfrage/t",
        company_name="PURA", contact_name="Daniel", with_formular_button=False,
    )
    assert "https://x/anfrage/t" not in txt           # ohne Button kein Link


def test_kunde_reply_text_slots_numbered():
    txt = build_kunde_reply_text(
        kunde_anrede_name="Sven", reply_text="Hier Vorschläge.", form_url="",
        company_name="PURA", contact_name="Daniel", with_formular_button=False,
        slot_proposals=[
            {"wochentag": "Do", "datum": "22.05.2026", "uhrzeit": "14:00"},
            {"wochentag": "Fr", "datum": "23.05.2026", "uhrzeit": "09:00"},
        ],
    )
    assert "1. Do, 22.05.2026 um 14:00 Uhr" in txt
    assert "2. Fr, 23.05.2026 um 09:00 Uhr" in txt


def test_kunde_reply_text_booked():
    txt = build_kunde_reply_text(
        kunde_anrede_name="", reply_text="ok", form_url="",
        company_name="PURA", contact_name="Daniel", with_formular_button=False,
        booked_termin={"datum": "22.05.2026", "uhrzeit": "14:00", "anliegen": "Küche"},
    )
    assert "Termin bestätigt: 22.05.2026 um 14:00 Uhr" in txt
    assert "Küche" in txt


def test_storno_text_plural_no_html():
    t = _build_storno_text(
        kunde_anrede="Herr Müller", company_name="PURA", cancelled_count=2,
    )
    assert "<" not in t
    assert "2 Termine wurden storniert" in t
    assert t.startswith("Hallo Herr Müller,")


def test_storno_text_zero_found():
    t = _build_storno_text(kunde_anrede="", company_name="PURA", cancelled_count=0)
    assert "keinen bestehenden Termin" in t


def test_verschiebung_text():
    t = _build_verschiebung_text(
        kunde_anrede="", company_name="PURA",
        found_termine=[{"datum": "22.05.2026", "uhrzeit": "14:00"}],
    )
    assert "<" not in t
    assert "am 22.05.2026 um 14:00 Uhr" in t


def test_buche_confirmation_text():
    t = _build_buche_confirmation_text(
        kunde_anrede="Sven", company_name="PURA", datum_label="22.05.2026",
        uhrzeit="14:00", employee_name="Daniel", anliegen="Küche",
        contact_phone="0211 1234",
    )
    assert "<" not in t
    assert "Termin: 22.05.2026 um 14:00 Uhr" in t
    assert "Daniel kommt am vereinbarten Termin." in t


# ===================================================================
# multipart/alternative-MIME
# ===================================================================

def test_mime_alternative_structure():
    b64 = _build_mime_alternative_b64(
        subject="Re: Test", to_email="kunde@gmx.de",
        body_html="<p>Hallo</p>", body_text="Hallo", cc=None,
    )
    msg = message_from_bytes(base64.b64decode(b64))
    assert msg.get_content_type() == "multipart/alternative"
    parts = msg.get_payload()
    assert len(parts) == 2
    # text zuerst, html zuletzt (in alternative gilt der letzte als bevorzugt)
    assert parts[0].get_content_type() == "text/plain"
    assert parts[1].get_content_type() == "text/html"
    assert msg["Subject"] == "Re: Test"
    assert msg["To"] == "kunde@gmx.de"


def test_mime_alternative_contents_and_cc():
    b64 = _build_mime_alternative_b64(
        subject="Hi", to_email="a@b.de", body_html="<p>HTML-Teil</p>",
        body_text="Text-Teil", cc=["c@d.de"],
    )
    msg = message_from_bytes(base64.b64decode(b64))
    parts = msg.get_payload()
    text = parts[0].get_payload(decode=True).decode("utf-8")
    html = parts[1].get_payload(decode=True).decode("utf-8")
    assert "Text-Teil" in text
    assert "<p>HTML-Teil</p>" in html
    assert msg["Cc"] == "c@d.de"


# ===================================================================
# Reply-Trimming (4 Quote-Formate + Edge-Cases)
# ===================================================================

def test_trim_gmx():
    t = ("Ja, der erste passt mir.\n\n"
         "Am 18.05.2026 um 14:30 schrieb Max Mustermann <max@gmx.de>:\n"
         "> alte nachricht\n> mehr davon")
    assert trim_quoted_reply(t) == "Ja, der erste passt mir."


def test_trim_gmail():
    t = ("Klingt gut, danke!\n\n"
         "On Mon, 18 May 2026 at 14:30 Max <max@gmail.com> wrote:\n"
         "> blabla")
    assert trim_quoted_reply(t) == "Klingt gut, danke!"


def test_trim_apple():
    t = ("Perfekt.\n\n"
         "On 18 May 2026, at 14:30, Max <max@me.com> wrote:\n"
         "alter inhalt")
    assert trim_quoted_reply(t) == "Perfekt."


def test_trim_outlook_von_block():
    t = ("Hier meine Antwort.\n\n"
         "Von: Max Mustermann <max@outlook.de>\n"
         "Gesendet: Montag, 18. Mai 2026 14:30\n"
         "An: Betrieb\nBetreff: Re: Termin\n\n"
         "Originaltext der vorherigen Mail …")
    assert trim_quoted_reply(t) == "Hier meine Antwort."


def test_trim_outlook_divider():
    t = ("Neue kurze Antwort\n"
         "________________________________\n"
         "Von: Max\nGesendet: heute\nBetreff: x")
    assert trim_quoted_reply(t) == "Neue kurze Antwort"


def test_trim_quoted_lines():
    t = "Passt!\n> vorherige zeile\n> noch eine"
    assert trim_quoted_reply(t) == "Passt!"


def test_trim_no_marker_unchanged():
    t = "Einfach nur eine Frage zu den Öffnungszeiten."
    assert trim_quoted_reply(t) == t


def test_trim_only_quote_keeps_original():
    t = "> nur ein zitat ohne neuen text"
    assert trim_quoted_reply(t) == t.strip()


def test_trim_von_without_header_is_no_marker():
    # "Von ..." ohne Folge-Header (kein Outlook-Block) -> nicht schneiden
    t = "Von mir aus gerne am Donnerstag, das passt super."
    assert trim_quoted_reply(t) == t
