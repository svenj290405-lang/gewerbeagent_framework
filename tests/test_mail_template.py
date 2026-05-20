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


def test_preheader_is_absent_anti_spam(basic_ctx):
    """Preheader-Text wurde bewusst entfernt (Anti-Spam, Marketing-
    Pattern triggert Filter). Inbox-Vorschau nimmt jetzt den ersten
    Body-Satz statt eines versteckten Marketing-Strings."""
    html = build_kunde_reply_html(**basic_ctx)
    assert "Dein Anfrage-Formular" not in html


def test_no_q_assistant_footer(basic_ctx):
    """'Verfasst mit Hilfe von Q'-Footer wurde entfernt (Anti-Spam,
    Auto-Bot-Marker). Layout bleibt sonst unveraendert."""
    html = build_kunde_reply_html(**basic_ctx)
    assert "Verfasst mit Hilfe von" not in html
    assert "digitalen Assistenten" not in html


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


def test_reply_text_url_is_stripped(basic_ctx):
    """Wenn der LLM trotz Prompt eine nackte URL in den Reply-Text
    schreibt: muss vom Template rausgestrippt werden, sonst rendert
    GMX die URL als grossen blauen Link der mit dem Button konkurriert.
    """
    basic_ctx["reply_text"] = (
        "danke fuer deine Anfrage.\n"
        "Bitte fuell das Formular aus: https://gewerbeagent.de/anfrage/EVIL_URL_LEAK\n"
        "Dann melde ich mich."
    )
    html = build_kunde_reply_html(**basic_ctx)
    # Nicht als sichtbarer Text — das Token aus dem Reply-Text darf nicht im Body auftauchen
    assert "EVIL_URL_LEAK" not in html
    # Form-URL aus basic_ctx bleibt im Button-href erhalten
    assert 'href="https://gewerbeagent.de/anfrage/AbC123XyZ789-token-hash-secret"' in html


def test_reply_text_inline_url_is_stripped(basic_ctx):
    """Inline-URL mitten in einem Satz: URL raus, Resttext bleibt."""
    basic_ctx["reply_text"] = (
        "Hier ist der Link dazu: https://example.com/leaked sehr wichtig."
    )
    html = build_kunde_reply_html(**basic_ctx)
    assert "example.com/leaked" not in html
    # Der umgebende Text bleibt lesbar
    assert "Hier ist der Link dazu" in html
    assert "sehr wichtig" in html


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


# =====================================================================
# build_kunde_reply_html — Phase-2 Termin-Aktionen
# (slot_proposals / booked_termin / storno_summary)
# =====================================================================

def test_slot_proposals_render_numbered_list(basic_ctx):
    """Bei slot_proposals wird eine durchnummerierte Box gerendert
    und KEIN Formular-Button (auch wenn with_formular_button=True)."""
    basic_ctx["with_formular_button"] = True
    basic_ctx["slot_proposals"] = [
        {"wochentag": "Do", "datum": "22.05.2026", "uhrzeit": "14:00"},
        {"wochentag": "Fr", "datum": "23.05.2026", "uhrzeit": "09:00"},
        {"wochentag": "Mo", "datum": "26.05.2026", "uhrzeit": "10:00"},
    ]
    html = build_kunde_reply_html(**basic_ctx)
    assert "Mögliche Termine" in html
    assert "22.05.2026" in html and "14:00" in html
    # Durchnummeriert 1./2./3. (NICHT 0-basiert — fuer den Kunden)
    assert "1." in html and "2." in html and "3." in html
    # Formular-Button ist NICHT da, obwohl with_formular_button=True
    assert "Formular ausfüllen" not in html


def test_booked_termin_renders_confirmation_box(basic_ctx):
    """booked_termin rendert eine 'Termin bestätigt'-Box."""
    basic_ctx["with_formular_button"] = False
    basic_ctx["booked_termin"] = {
        "datum": "22.05.2026",
        "uhrzeit": "14:00",
        "anliegen": "Küchenmontage",
    }
    html = build_kunde_reply_html(**basic_ctx)
    assert "Termin bestätigt" in html
    assert "22.05.2026" in html and "14:00" in html
    assert "Küchenmontage" in html


def test_storno_summary_one_cancelled(basic_ctx):
    basic_ctx["with_formular_button"] = False
    basic_ctx["storno_summary"] = {"cancelled_count": 1}
    html = build_kunde_reply_html(**basic_ctx)
    assert "Termin storniert" in html


def test_storno_summary_zero_cancelled_shows_not_found(basic_ctx):
    basic_ctx["with_formular_button"] = False
    basic_ctx["storno_summary"] = {"cancelled_count": 0}
    html = build_kunde_reply_html(**basic_ctx)
    assert "Termin nicht gefunden" in html
    # Keine "0 Termine storniert"-Phrase (klingt absurd)
    assert "0 Termine storniert" not in html


def test_storno_summary_multiple_uses_plural(basic_ctx):
    basic_ctx["with_formular_button"] = False
    basic_ctx["storno_summary"] = {"cancelled_count": 3}
    html = build_kunde_reply_html(**basic_ctx)
    assert "3 Termine storniert" in html


def test_booked_termin_with_button_shows_both(basic_ctx):
    """Neuer Flow: nach der Buchung wird die Termin-Bestaetigung UND der
    Formular-Button zusammen gerendert (erst Termin buchen, dann das
    Anfrage-Formular gleich mitschicken)."""
    basic_ctx["with_formular_button"] = True
    basic_ctx["booked_termin"] = {
        "datum": "22.05.2026", "uhrzeit": "14:00", "anliegen": "X",
    }
    html = build_kunde_reply_html(**basic_ctx)
    assert "Termin bestätigt" in html
    assert "Formular ausfüllen" in html


def test_booked_termin_without_button_no_formular(basic_ctx):
    """Buchung OHNE with_formular_button: nur die Termin-Bestaetigung,
    kein Formular-Button (z.B. wenn schon ein Formular eingegangen ist)."""
    basic_ctx["with_formular_button"] = False
    basic_ctx["booked_termin"] = {
        "datum": "22.05.2026", "uhrzeit": "14:00", "anliegen": "X",
    }
    html = build_kunde_reply_html(**basic_ctx)
    assert "Termin bestätigt" in html
    assert "Formular ausfüllen" not in html


def test_no_termin_block_means_normal_formular_path(basic_ctx):
    """Ohne slot/booked/storno -> default Verhalten unveraendert."""
    html = build_kunde_reply_html(**basic_ctx)
    assert "Formular ausfüllen" in html
    assert "Mögliche Termine" not in html
    assert "Termin bestätigt" not in html


def test_slot_proposals_escape_xss(basic_ctx):
    """Slot-Felder werden HTML-escaped (kommen aus dem kalender-Plugin,
    aber defensive)."""
    basic_ctx["with_formular_button"] = False
    basic_ctx["slot_proposals"] = [
        {"wochentag": "<script>", "datum": "22.05.2026", "uhrzeit": "14:00"},
    ]
    html = build_kunde_reply_html(**basic_ctx)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_slot_proposals_caps_at_six_entries(basic_ctx):
    """Mehr als 6 Slots werden abgeschnitten (textlich unhandlich)."""
    basic_ctx["with_formular_button"] = False
    basic_ctx["slot_proposals"] = [
        {"wochentag": "Mo", "datum": f"0{i}.06.2026", "uhrzeit": "10:00"}
        for i in range(1, 10)
    ]
    html = build_kunde_reply_html(**basic_ctx)
    # Erste 6 sind drin, der 7. nicht
    assert "06.06.2026" in html
    assert "07.06.2026" not in html
