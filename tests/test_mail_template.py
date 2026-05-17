"""Tests fuer mail_template.build_kunde_reply_html (UX-Lift 2026-05-17).

Wichtigste Anti-Scam-Garantien:
- Voller Token-URL wird NICHT als Text gerendert (nur als Button-href)
- Stattdessen Domain-Hint ("auf gewerbeagent.de")
- Inhaber-Name sichtbar (kein anonymes "Support-Team")
- Trust-Box mit drei Vertrauens-Signalen
"""
from __future__ import annotations

import pytest

from core.integrations.mail_template import (
    _extract_display_domain,
    build_kunde_reply_html,
    extract_first_name,
)


# =====================================================================
# extract_first_name (regression — unveraendert)
# =====================================================================

def test_extract_first_name_from_full_name():
    assert extract_first_name("Sven Jantos") == "Sven"


def test_extract_first_name_strips_titles():
    assert extract_first_name("Dr. Max Müller") == "Max"


def test_extract_first_name_from_email_with_dot():
    assert extract_first_name("maria.mueller@example.com") == "Maria"


def test_extract_first_name_generic_aliases_return_empty():
    for alias in ("info@firma.de", "kontakt@firma.de", "noreply@firma.de"):
        assert extract_first_name(alias) == ""


def test_extract_first_name_empty_input():
    assert extract_first_name("") == ""
    assert extract_first_name(None) == ""


# =====================================================================
# _extract_display_domain
# =====================================================================

def test_display_domain_strips_protocol_and_path():
    assert _extract_display_domain(
        "https://gewerbeagent.de/anfrage/abc-token-xyz",
    ) == "gewerbeagent.de"


def test_display_domain_strips_www_prefix():
    assert _extract_display_domain(
        "https://www.example.com/anfrage/x",
    ) == "example.com"


def test_display_domain_keeps_subdomain():
    """Subdomains (anfrage.example.com) bleiben als Vertrauenssignal."""
    assert _extract_display_domain(
        "https://anfrage.gewerbeagent.de/path",
    ) == "anfrage.gewerbeagent.de"


def test_display_domain_falls_back_on_bad_input():
    assert _extract_display_domain("nicht-eine-url") == "unserer Website"


# =====================================================================
# build_kunde_reply_html — Anti-Scam-Garantien
# =====================================================================

@pytest.fixture
def basic_ctx():
    return dict(
        kunde_anrede_name="Frau Mueller",
        kunde_email="mueller@example.com",
        reply_text="vielen Dank fuer deine Anfrage.\nIch habe das vermerkt.",
        form_url="https://gewerbeagent.de/anfrage/AbC123XyZ789-token-hash-secret",
        company_name="PURA Tischler",
        contact_name="Daniel Tombers",
        contact_email="daniel@pura-tischler.de",
        contact_phone="+49 30 1234 56",
        contact_website="pura-tischler.de",
    )


def test_token_url_not_visible_as_text(basic_ctx):
    """Der volle Token-URL darf NICHT als Text im Body stehen.
    Anti-Scam: nur die Domain ist als 'auf X.Y'-Hint sichtbar."""
    html = build_kunde_reply_html(**basic_ctx)
    token = "AbC123XyZ789-token-hash-secret"
    # In href OK (im Button), nicht als sichtbarer Text
    # Suche das Token NUR im href des Buttons, nicht als Display-Text.
    # Heuristik: token darf nicht direkt zwischen > und < stehen
    # (= als HTML-Textinhalt). Aber wir wollen es im href-Attribut sehen.
    assert f'href="https://gewerbeagent.de/anfrage/{token}"' in html
    # Display-Text darf den Token nicht enthalten
    # (zwischen >...< vorkommen) — wir nutzen einen einfachen Check:
    # die offensichtlichste Stelle wo's vorher stand war
    # `<a href="..." style="...">{form_url}</a>` mit dem URL als Text.
    assert f">{basic_ctx['form_url']}</a>" not in html
    assert f">{basic_ctx['form_url']}<" not in html


def test_domain_hint_no_longer_shown(basic_ctx):
    """Domain-Hint wurde durch Website-Button ersetzt — kein 'auf X.Y'
    mehr im Display-Text (User-Feedback: zu prominent)."""
    html = build_kunde_reply_html(**basic_ctx)
    # 'auf gewerbeagent.de' war der alte Hint — darf nicht mehr da sein
    assert "auf gewerbeagent.de" not in html


def test_contact_name_visible_in_header(basic_ctx):
    """Inhaber-Name im Header sichtbar (kein anonymes 'Support-Team')."""
    html = build_kunde_reply_html(**basic_ctx)
    assert "Daniel Tombers" in html


def test_company_name_visible(basic_ctx):
    html = build_kunde_reply_html(**basic_ctx)
    assert "PURA Tischler" in html


def test_initials_avatar_uses_first_letters_of_contact(basic_ctx):
    html = build_kunde_reply_html(**basic_ctx)
    # "Daniel Tombers" -> "DT" — Avatar-Div rendert Whitespace drum, also
    # nach Stripping vergleichen.
    import re
    # Suche das Avatar-Div und extrahiere seinen Inhalt
    match = re.search(
        r'border-radius:\s*50%[^>]*>\s*([A-Z·]+)\s*</div>', html,
    )
    assert match is not None, "Kein Avatar-Div mit Initialen gefunden"
    assert match.group(1) == "DT"


def test_no_unkept_promises_in_body(basic_ctx):
    """KEINE Versprechen die wir technisch nicht garantieren koennen:
    nicht 'DSGVO-konform' (rechtliche Behauptung), nicht
    'Antwort innerhalb 24h' (operatives Versprechen)."""
    html = build_kunde_reply_html(**basic_ctx)
    assert "DSGVO-konform" not in html
    assert "innerhalb 24" not in html
    assert "24 h" not in html
    assert "24h" not in html


def test_personal_greeting_uses_first_name(basic_ctx):
    html = build_kunde_reply_html(**basic_ctx)
    assert "Hallo Frau Mueller" in html


def test_empty_anrede_uses_generic_hallo(basic_ctx):
    basic_ctx["kunde_anrede_name"] = ""
    html = build_kunde_reply_html(**basic_ctx)
    assert "Hallo," in html


def test_button_uses_form_url_href(basic_ctx):
    """Button-href enthaelt die echte Token-URL (sonst klappt der Link nicht)."""
    html = build_kunde_reply_html(**basic_ctx)
    assert f'href="{basic_ctx["form_url"]}"' in html


def test_preheader_is_present_but_hidden(basic_ctx):
    """Preheader-Text fuer Inbox-Vorschau, im Body unsichtbar."""
    html = build_kunde_reply_html(**basic_ctx)
    assert "Dein Anfrage-Formular" in html
    assert "display: none" in html  # Preheader-Hide-CSS


def test_reply_text_anrede_is_stripped(basic_ctx):
    """Wenn Gemini selbst 'Hallo X' generiert, wird die innere Anrede
    entfernt damit nicht doppelt 'Hallo' steht."""
    basic_ctx["reply_text"] = (
        "Hallo Frau Mueller,\n\n"
        "vielen Dank fuer Ihre Anfrage."
    )
    html = build_kunde_reply_html(**basic_ctx)
    # Aeussere Anrede ist 1x da, Gemini-Hallo ist raus
    assert html.count("Hallo Frau Mueller") == 1


def test_reply_text_signature_is_stripped(basic_ctx):
    """Wenn Gemini selbst 'Viele Gruesse, Daniel' anhaengt, wird das raus."""
    basic_ctx["reply_text"] = (
        "vielen Dank fuer deine Anfrage.\n\n"
        "Viele Gruesse\n"
        "Daniel (via Q)"
    )
    html = build_kunde_reply_html(**basic_ctx)
    # 'Daniel' steht weiter im Header/Footer, aber das 'via Q' aus
    # dem Reply-Text ist weg
    assert "(via Q)" not in html


def test_contact_phone_in_footer(basic_ctx):
    html = build_kunde_reply_html(**basic_ctx)
    assert "+49 30 1234 56" in html


def test_no_website_button(basic_ctx):
    """Website-Button wurde entfernt (Iteration 3, User-Feedback): Mail
    sollte nur den Formular-Button haben, keinen zweiten Button."""
    html = build_kunde_reply_html(**basic_ctx)
    assert "Zur Website" not in html
    # contact_website darf auch nicht im href irgendwo auftauchen
    assert "pura-tischler.de" not in html.replace("daniel@pura-tischler.de", "")


def test_no_raw_urls_visible_in_body(basic_ctx):
    """Kein 'https://'-String als Display-Text — nur in href-Attributes.
    Das schliesst Form-URL und Website ein."""
    html = build_kunde_reply_html(**basic_ctx)
    # Schneide alle href-Werte raus, der Rest darf kein https:// enthalten
    import re
    html_no_hrefs = re.sub(r'href="[^"]*"', 'href=""', html)
    # Auch keinen sichtbaren mailto-Link
    html_no_hrefs = re.sub(r'href="mailto:[^"]*"', 'href=""', html_no_hrefs)
    assert "https://" not in html_no_hrefs
    assert "http://" not in html_no_hrefs


def test_no_contact_extras_doesnt_crash(basic_ctx):
    """Wenn phone/mail/web leer sind: Mail rendert trotzdem ohne Crash."""
    basic_ctx["contact_phone"] = ""
    basic_ctx["contact_email"] = ""
    basic_ctx["contact_website"] = ""
    html = build_kunde_reply_html(**basic_ctx)
    assert "PURA Tischler" in html  # Firmen-Footer noch da


def test_xss_in_company_name_is_escaped(basic_ctx):
    """User-kontrollierte Felder (company_name etc) werden escaped."""
    basic_ctx["company_name"] = "<script>alert(1)</script>"
    html = build_kunde_reply_html(**basic_ctx)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_html_is_valid_doctype(basic_ctx):
    html = build_kunde_reply_html(**basic_ctx)
    assert html.startswith("<!doctype html>")
    assert html.rstrip().endswith("</html>")
