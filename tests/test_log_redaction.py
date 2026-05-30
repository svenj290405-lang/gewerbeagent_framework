"""Tests fuer den Log-Redaction-Filter (_redact_secrets / RedactingFormatter).

Deckt PII (E-Mail im Klartext UND URL-kodiert) + Secrets (Telegram-Token)
ab. Der URL-kodierte Fall ist S14 (siehe core/logging_context.py): "@" als
"%40" in geloggten Google-API-Fehler-URLs rutschte vorher ungeschwaerzt
durch.
"""
from core.logging_context import _redact_secrets


def test_plaintext_email_redacted():
    out = _redact_secrets("Kontakt max@firma.de gebucht")
    assert "max@firma.de" not in out
    assert "m***@firma.de" in out


def test_urlencoded_email_redacted():
    # S14: das "@" steht als "%40"
    out = _redact_secrets("q=svenj05%40gmx.de")
    assert "svenj05%40gmx.de" not in out
    assert "s***%40gmx.de" in out


def test_find_events_error_url_redacted():
    # Exakt das Muster aus den realen find_events-400-WARNINGs
    url = (
        "privateExtendedProperty=kunde_email%3Dsvenj05%40gmx.de"
        "&q=svenj05%40gmx.de&alt=json"
    )
    out = _redact_secrets(url)
    # Endkunden-Adresse darf in KEINER Form mehr auftauchen
    assert "svenj05%40gmx.de" not in out
    assert "svenj05" not in out
    # strukturelle Reste (kein PII) bleiben erhalten
    assert "alt=json" in out


def test_phone_redacted():
    out = _redact_secrets("Anrufer +4915112345678 meldet sich")
    assert "+4915112345678" not in out
    assert "<tel-redacted>" in out


def test_phone_formatted_with_separators_redacted():
    # Formatierte Nummern (Leerzeichen/Slash) wurden vorher NICHT maskiert.
    for raw in ("0211 / 87 65 43 21", "+49 211 8765432", "(0211) 876-543"):
        out = _redact_secrets(f"Kunde Telefon {raw} notiert")
        assert "<tel-redacted>" in out, raw
        assert "876" not in out, raw


def test_version_string_not_redacted_as_phone():
    # Punkt-getrennte Zahlenketten (Versionen) duerfen NICHT als Tel gelten.
    txt = "Build-Version 0.1.2.3.4.5.6.7.8.9 geladen"
    assert _redact_secrets(txt) == txt


def test_telegram_token_redacted():
    out = _redact_secrets(
        "GET api.telegram.org/bot7654321:AAFhijklmnopqrstuvwxyz0123456789abcd/x"
    )
    assert "AAFhijklmnopqrst" not in out
    assert "bot<redacted>" in out


def test_no_false_positive_on_plain_text():
    # Kein "@", kein "%40", keine Nummer -> unveraendert
    txt = "Cron-Lauf fertig: 1 offene Tokens geprueft"
    assert _redact_secrets(txt) == txt
