"""Tests fuer den Log-Redaction-Filter (_redact_secrets / RedactingFormatter).

Deckt PII (E-Mail im Klartext UND URL-kodiert) + Secrets (API-Token)
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


def test_api_token_redacted():
    out = _redact_secrets(
        "GET https://api.example.com/7654321:AAFhijklmnopqrstuvwxyz0123456789abcd/x"
    )
    assert "AAFhijklmnopqrst" not in out
    assert "<redacted-token>" in out


def test_no_false_positive_on_plain_text():
    # Kein "@", kein "%40", keine Nummer -> unveraendert
    txt = "Cron-Lauf fertig: 1 offene Tokens geprueft"
    assert _redact_secrets(txt) == txt


# =====================================================================
# Namen und Suchbegriffe in Query-Strings
# =====================================================================
# Aufgefallen im Audit am 2026-08-23: im Zugriffs-Log stand
# "GET /app/api/archiv/dateien?kunde=<Klarname>". Der uvicorn-Logger lief
# an diesem Formatter vorbei (eigene Handler, kein propagate), und ein
# Name ist ausserdem von keinem der bestehenden Muster erfasst. Beides
# ist gefixt — hier die Absicherung.

def test_kundenname_im_query_string_wird_maskiert():
    zeile = 'GET /app/api/archiv/dateien?kunde=Henrik%20Anton HTTP/1.1" 200'
    aus = _redact_secrets(zeile)
    assert "Henrik" not in aus
    assert "kunde=<redacted>" in aus


def test_suchbegriff_wird_maskiert_rest_der_url_bleibt():
    aus = _redact_secrets("GET /app/api/kunden?suche=Mueller&limit=5")
    assert "Mueller" not in aus
    assert "limit=5" in aus, "Nur der PII-Parameter darf verschwinden"


def test_harmlose_parameter_bleiben_lesbar():
    zeile = "GET /app/api/auftraege?status=offen&tage=14"
    assert _redact_secrets(zeile) == zeile


def test_uvicorn_logger_schreibt_ueber_den_root():
    """Ohne das lief der Zugriffs-Log komplett an der Maskierung vorbei."""
    import logging
    from core.logging_context import configure_structured_logging

    configure_structured_logging()
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        log = logging.getLogger(name)
        assert log.propagate is True, f"{name} leitet nicht an den Root weiter"
        assert not log.handlers, f"{name} hat noch eigene Handler"
